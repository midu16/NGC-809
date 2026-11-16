# CRs and tools to re-create the NGC-809 25 % / 75 % SR-IOV VF split tests

This directory has everything needed to rebuild and re-run the testing: 
- **VF0 = 25 %** (6250 Mbit/s): `iperf3`. Since 2026-11-16 this is a **guaranteed minimum only**: `minTxRate: 6250`, with `maxTxRate` removed;
- **VF1 = 75 %** (18750 Mbit/s): `TRex → testpmd → TRex`. Hard limit: `minTxRate = maxTxRate = 18750`;
- both together on the 25G `ens1f0np0` uplinks of **master-0** and **appworker-1**.

---

## 1. Contents

| Path | What | Objects |
|---|---|---|
| `01-namespace.yaml` | Namespace | `dpdk` |
| `02-nncp-vf-partition.yaml` | NMState: 2 VFs on `ens1f0np0`; VF0 0/0 (no cap; the min 6250 comes from sriov-cni, §3.2), VF1 18750/18750; trust on; spoof-check off | NNCP `ngc809-master0-nic2-vf-partition` (master-0), `ngc809-nic2-vf-partition` (appworker-1) |
| `03-sriovnetworknodepolicies.yaml` | SR-IOV resources (`externallyManaged: true`, `netdevice`) | `ngc809-master0-nic2-vf` (m0 VF1 → `ngc809_srv_nic2_vf`, RDMA), `ngc809-nic2-vf1-tenant` (aw1 VF1 → `ngc809_nic2_vf1`, RDMA), `ngc809-m0-vf0-iperf` (m0 VF0 → `ngc809_m0_vf0`), `ngc809-aw1-vf0-iperf` (aw1 VF0 → `ngc809_aw1_vf0`) |
| `04-sriovnetworks.yaml` | Attachments, generating the NADs in `dpdk` | `ngc809-tenant-75` and `ngc809-master0-sink` (VLAN 206, trust on, 18750/18750, host-local), `ngc809-vf0-25-aw1` and `ngc809-vf0-25-m0` (VLAN 206, trust off, `minTxRate: 6250` only, static 10.10.30.2 / .1) |
| `05-configmaps-trex.yaml` | TRex config template, `PORT_BANDWIDTH_GB=25`, STL profile | `trex-config-template`, `trex-info-for-config`, `trex-tests` |
| `06-pods-trex-testpmd.yaml` | VF1 workloads | `trex` (appworker-1, `quay.io/midu/trex:2.87-ofed5.8`, 4 exclusive CPUs, 2 Gi 1G-hugepages, runtimeClass `performance-pao-worker-profile-appworker-mcp-a`), `testpmd` (master-0, `…/nrho-26.8/rhel9/testpmd:25.11-mlx5`) |
| `07-pods-iperf3.yaml` | VF0 workloads | `iperf3-server` (master-0, `iperf3 -s -B 10.10.30.1`), `iperf3-client` (appworker-1, `sleep infinity`). Image: `quay.io/midu/iperf3@sha256:6e84f0fd…cb05c` |
| `pod-files/trex/` | Files that live only inside the trex container | `/etc/trex_cfg.yaml`, `/tmp/trex_e2e_run.py` (the TRex test driver) and other helpers |
| `pod-files/testpmd/` | Files that live only inside the testpmd container | `/tmp/run_testpmd_bigring.sh` (mac-forward, promisc off, 4096 descriptors), `run_testpmd.sh`, `run_txonly*.sh` |
| `scripts/` | Test drivers, run from the laptop through the hub | `ngc809_iperf_split_test.sh`, `ngc809_concurrent_split_test.sh`, `ngc809_e2e_cap_test.sh`, per-second samplers, `trex_e2e_run.py` |
| `tools/apply.sh` | Ordered, waiting re-create of 01–07, then file restore | — |
| `tools/restore_pod_files.sh` | Copy `pod-files/` into the pods; check TRex CPUs and VF against `trex_cfg.yaml` | — |
| `tools/start_apps.sh` | Start the TRex server and testpmd, re-learn MACs, 64 B sanity run; `stop` stops testpmd and clears PROMISC | — |
| `kustomize/` | Kustomize form of 01–07, using the same layout as [telco-reference `example-overlays-config`](https://github.com/openshift-kni/telco-reference/tree/main/telco-hub/configuration/example-overlays-config). All site values sit in one file per overlay, `site-values.yaml` (§3.1) | `reference-crs/` (base, placeholders), `components/site-values/` (replacements), `example-overlays-config/{cwl,template}/` |
| `tools/render.py` | Renders an overlay into staged 01–07 files plus `expected-state.json` under `rendered/<site>/`. Applies the per-VF **rate mode** (§3.2). Fails on any unresolved `<placeholder>`. `--check` compares the output with the root 01–07 | — |
| `tools/rate_rollout.py` | Rolls a rate profile out to CWL: render → plan → apply NNCP/SriovNetwork → check OpenShift + hosts → re-create the pods → check again → start the apps → README §5 tests (§3.2) | — |
| `tools/clean_export.py` | Regenerates 01–07 and `_reference/` from `_live-export/` | — |
| `_reference/` | **Do not apply.** Kept for context | the generated NADs; `SriovOperatorConfig default` (`disableDrain: true`); `PerformanceProfile pao-worker-profile-appworker-mcp-a` |
| `_live-export/` | Raw `oc get -o json` of every object, host `ip -d link show ens1f0np0` from both nodes, operator CSV versions | evidence |

---

## 2. Prerequisites (already in place on CWL; not created by these files)

- **Cluster and operators:**
  - OCP 4.22.3;
  - `sriov-network-operator.v4.22.0-202608122145`;
  - `kubernetes-nmstate-operator.4.22.0-202608130510`.
- **SR-IOV operator config:** `SriovOperatorConfig default` with `disableDrain: true`, `disablePlugins: [mellanox]`, `enableInjector: true` (see `_reference/`).
- **appworker-1 tuning:** `PerformanceProfile pao-worker-profile-appworker-mcp-a`, giving isolated CPUs, 24 × 1G hugepages, `single-numa-node` topology, the static CPU manager and runtimeClass `performance-pao-worker-profile-appworker-mcp-a` (see `_reference/`). The trex pod needs it. master-0 has no profile, so testpmd runs with `--no-huge` on shared CPUs.
- **Hardware:** ConnectX-6 Lx `ens1f0np0` (`0000:12:00.0`) on both nodes, in legacy eswitch mode. VFs: `0000:12:00.2` = VF0, `0000:12:00.3` = VF1.
- **Switch:** leaf_a-sw-706, ports `WGE1/0/10:1` (appworker-1) and `WGE1/0/16:1` (master-0). Both are trunks permitting **VLAN 206**.
- **Images:**
  - The cluster pulls `quay.io/midu/trex:2.87-ofed5.8` and `quay.io/midu/iperf3@sha256:6e84…` from quay.io;
  - testpmd comes from the hub Quay mirror.
- **Access:**
  - All tools run from this laptop through `ssh ngc-747-hub` with `KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml`.
  - Pods must be created as **cluster-admin**, because they are privileged. The privileged SCC admits `system:admin`; there is no namespace RBAC.

---

## 3. Re-create

```bash
tools/apply.sh          # 01 → 07 with waits, then tools/restore_pod_files.sh
tools/start_apps.sh     # TRex server + testpmd (promisc off), MAC re-learn, 64 B sanity run
```

Manual equivalent, in this order:
1. `oc apply -f 01-namespace.yaml`.
2. `oc apply -f 02-nncp-vf-partition.yaml`. Wait until both NNCPs are **Available**.
3. `oc apply -f 03-sriovnetworknodepolicies.yaml`. Wait until both `SriovNetworkNodeState`s are **Succeeded** **and** all four resources show `1` in the nodes' allocatable.
4. `oc apply -f 04-sriovnetworks.yaml`. Wait for the 4 NADs in `dpdk`.
5. `oc apply -f 05-configmaps-trex.yaml`.
6. `oc apply -f 06-pods-trex-testpmd.yaml -f 07-pods-iperf3.yaml`. Wait until the pods are Ready.
7. `tools/restore_pod_files.sh`.

Expected host state on **both** nodes (`ip link show ens1f0np0`). `ip link` does not print a rate that is 0:
```
vf 0  …, vlan 206, min_tx_rate 6250Mbps,  spoof checking off, trust off                          (iperf3 pod)
vf 1  …, vlan 206, max_tx_rate 18750Mbps, min_tx_rate 18750Mbps, spoof checking off, trust on    (trex / testpmd pod)
```

### 3.1 Kustomize overlay (templating for other sites)

The root 01–07 files are the live export. `kustomize/` produces the same 18 objects from a generic base plus **one site file**:

```
kustomize/
├── reference-crs/              # base: the 7 manifests, site values replaced by "<placeholder>" (integers: 0)
│   ├── kustomization.yaml      #   + configMapGenerator for trex-config-template / trex-tests (bodies in files/)
│   └── files/                  #   trex_cfg.yaml, testpmd.py, testpmd_addr.py
├── components/site-values/     # kustomize Component: replacements SiteValues field -> every target field
└── example-overlays-config/
    ├── cwl/                    # CWL: kustomization.yaml + site-values.yaml (live values of 2026-11-11)
    └── template/               # copy to <site>/ and fill in site-values.yaml
```

- **`site-values.yaml`** is a `SiteValues` object marked `config.kubernetes.io/local-config`. It is never output or applied.
- It holds every site-specific value:
  - namespace;
  - server/client hostnames;
  - client runtimeClass and nodeSelector;
  - PF name, vendor/device ID and VF count;
  - port bandwidth and VLAN;
  - VF0/VF1 rates;
  - iperf3 IPs and VF1 IPAM subnets;
  - TRex/testpmd MACs;
  - images;
  - TRex CPUs and hugepages.
- Values inside strings are replaced with kustomize `delimiter`/`index`: the Multus annotation JSON, the IPAM JSON, `<pf>#1-1` in `pfNames`, the MACs in `testpmd*.py`, and the `-B` address of `iperf3 -s`.
- These stay fixed in the base: object names, SR-IOV `resourceName`s (they are map keys in the pod resources), trust/spoof-check, and the L2-irrelevant IPs inside `trex_cfg.yaml` and `testpmd_addr.py`.
- Keep the types used in `cwl/site-values.yaml`. Integers stay unquoted. `vendorId`, `deviceId`, `portBandwidthGb` and `trex.cpus` are quoted strings.

```bash
kubectl kustomize kustomize/example-overlays-config/cwl                    # view
python3 tools/render.py kustomize/example-overlays-config/cwl --check      # -> rendered/cwl/, verify == live export
tools/apply.sh rendered/cwl                                                # staged apply (same waits as §3)
```

Do **not** run `oc apply -k` on the overlay. It applies everything at once, and pitfalls 1 and 3 (§4) need the staged order. For a new site, copy `template/` to `<site>/`, fill in `site-values.yaml` and render it. `render.py` refuses to write output while any placeholder is left.

### 3.2 Rate modes and the rollout automation

kustomize can only set values. It cannot delete a field, so `tools/render.py` applies the tx-rate policy of each VF after `kubectl kustomize`. Set the mode in `site-values.yaml` (`spec.vf0.rateMode`, `spec.vf1.rateMode`), or override it with `--vf0-mode/--vf1-mode` (and the rate with `--vf0-rate/--vf1-rate`):

| Mode | SriovNetwork | NNCP | Host VF once the pod is attached |
|---|---|---|---|
| `enforced` | `minTxRate = maxTxRate = rate` | `min-tx-rate = max-tx-rate = rate` | min = max = rate |
| `min-only` | `minTxRate = rate`, **no** `maxTxRate` | `min-tx-rate: 0`, `max-tx-rate: 0` | min = rate, no cap (programmed by sriov-cni over netlink) |
| `removed` | neither field | both `0` | no limits |

The NNCP always gets explicit `0`s. nmstate leaves a key it is not given unchanged, so leaving the key out would keep the old limit on the NIC.

In `min-only` mode, the NNCP cannot carry the guarantee. Two attempts on CWL (nmstate 2.2.60) failed, and nmstate rolled both back:
- `min-tx-rate: 6250` with `max-tx-rate: 0` is rejected by NetworkManager: `invalid VF 0: min_tx_rate is greater than max_tx_rate`.
- `min-tx-rate: 6250` with no max key fails verification: `vfs[0].min-tx-rate desire '6250', current '0'`.

So the host baseline is 0/0, and sriov-cni programs min 6250 / max 0 when the iperf3 pod attaches. A `kubectl kustomize` view still shows `rateMbps` in both fields; only `render.py` applies the modes.

The CWL default (`cwl/site-values.yaml`, and the root 02/04) is **VF1 `enforced` 18750, VF0 `min-only` 6250**.

```bash
tools/rate_rollout.py plan                         # oc diff of 02/03/04 against CWL, changes nothing
tools/rate_rollout.py all --yes                    # apply + verify + re-deploy pods + verify + start apps + tests
tools/rate_rollout.py all --yes --vf1-mode removed # same, with the VF1 limits removed (rendered/cwl-vf1-removed/)
tools/rate_rollout.py all --yes --vf0-mode enforced  # back to the 2026-11-11 6250/6250 cap
tools/rate_rollout.py verify                       # check the cluster + hosts against the render
tools/rate_rollout.py test --tests iperf,conc,vf1 --out runs/<name>
```

**VF0 VLAN.** `spec.vf0.vlan` in `site-values.yaml` (default `spec.vlan` = 206), or `--vf0-vlan 306` on `render.py` / `rate_rollout.py`, sets the VLAN of the two VF0 SriovNetworks (`ngc809-vf0-25-*`). `spec.allowedVlans: [206, 306]` lists the VLANs that the switch trunks must permit. Any other value is rejected at render time. A VLAN change only touches 04 (not the NNCP or the SNNPs). The rollout re-creates the pods so that sriov-cni programs the new VLAN, and `verify` checks the VLAN in the SriovNetwork, the NAD and host `ip link`.

```bash
tools/rate_rollout.py all --yes --vf0-vlan 306     # VF0 iperf3 on VLAN 306, VF1 stays on 206 (rendered/cwl-vf0-vlan306/)
tools/rate_rollout.py all --yes                    # back to the site-values default (206)
```

What `apply` does:
- It refuses to run if the 03 SriovNetworkNodePolicies differ from the cluster (pitfall 1).
- It deletes the four test pods, because a running pod keeps the rates it attached with.
- It applies 02 and waits until each NNCE is `Available` at the new NNCP generation, then checks host `ip link` against the NNCP rates.
- It applies 04 and waits until the NADs carry exactly the expected `min_tx_rate`/`max_tx_rate` keys.
- It waits for the VF resources, then re-creates the trex/testpmd/iperf3 pods.
- It restores `pod-files/`. If the trex pod received other CPUs or another VF, it writes an adjusted `/etc/trex_cfg.yaml` into the pod.

`verify` compares the SriovNetwork spec, the NAD config, the NNCP spec, the NNS and host `ip link` (rates, VLAN, trust) with `expected-state.json`.

`test` runs T1–T6 (iperf3, VF0 alone), `tools/start_apps.sh`, C1/C2 (concurrent), then stops testpmd and runs the VF1 e2e loop (3.3). The logs and `SUMMARY.txt` go to `runs/<timestamp>-<name>/`, which git ignores.

`tools/apply.sh` takes the manifest directory as an argument. Its wait checks still use the CWL hostnames, and the restore/start/test tools under `tools/` and `scripts/` still hardcode the CWL values.

---

## 4. Pitfalls 

1. **Any create or change of a `SriovNetworkNodePolicy` rebinds VFs attached to running pods.**
   - `sriov-network-config-daemon` re-applies the PF. It cannot see VFs that are inside pod namespaces (`VFIsReady(): failed to get interface name`), so it runs `RebindVfToDefaultDriver()`.
   - The pods lose `net1`, while their annotations still show it.
   - Fix: apply 03 **before** the pods. After any later policy change, delete and re-create the pods.
2. **Re-applying an NNCP resets VF fields that sriov-cni set and the NNCP does not list,** such as VLAN 206 on a pod's VF. Re-create the affected pods afterwards, or restore by hand with `ip link set ens1f0np0 vf N vlan 206`.
3. **Pods are pinned with `nodeName`, which bypasses the scheduler.** If they start before the device plugin registers the resource, they get no VF, and sriov-cni fails with `VF pci addr is required`. That's why `apply.sh` waits for the allocatable resources first.
4. **A trusted VF in promiscuous mode makes the switch block the port.** It reflects the switch's BPDUs, so leaf_a puts the port into STP DISCARDING (`Loopback-Protected`) on all VLANs. testpmd enables promiscuous mode by default, so always use `run_testpmd_bigring.sh` (`set promisc all off`) and `tools/start_apps.sh stop`. Never disable STP on the switch port.
5. **`trex_cfg.yaml` is tied to the CPUs and VF the pod received** (`3-4,67-68`, `0000:12:00.3`). `restore_pod_files.sh` warns if a new pod received others; adjust the file before starting `t-rex-64`.
6. **The switch forgets the DPDK MACs after 300 s idle.** `start_apps.sh` and the test drivers send one ARP from each `net1` before every run.
7. **TRex rates must be measured on the hardware.** On a capped VF, TRex stretches its run instead of dropping frames. Use the hardware per-second samplers, or TRex's `*_wall` fields, not rates computed over the nominal duration.
8. **testpmd's default 256-descriptor TX ring drops frames** at the cap. Use `--txd=4096 --rxd=4096`, as `run_testpmd_bigring.sh` does.

---

## 5. Re-run the tests 

Each driver prints per-second VF0/VF1/PHY hardware rates for both nodes and a steady-state summary.

| Proof § | Test | Command |
|---|---|---|
| 3.2 T1 | VF0 25 %, aw1 → m0 | `scripts/ngc809_iperf_split_test.sh T1_tcp_fwd_P4 <out> -- -P 4 -t 20` |
| 3.2 T2 | VF0 25 %, m0 → aw1 | `scripts/ngc809_iperf_split_test.sh T2_tcp_rev_P4 <out> -- -P 4 -t 20 -R` |
| 3.2 T3 | VF0 25 %, both directions | `scripts/ngc809_iperf_split_test.sh T3_tcp_bidir_P4 <out> -- -P 4 -t 20 --bidir` |
| 3.2 T4 | VF0 25 %, UDP 12 G offered | `scripts/ngc809_iperf_split_test.sh T4_udp_fwd_12G <out> -- -u -b 3G -P 4 -l 1448 -t 20` |
| 3.2 T5/T6 | 1 vs 8 streams | `… T5_tcp_fwd_P1 <out> -- -P 1 -t 20` / `… T6_tcp_fwd_P8 <out> -- -P 8 -t 20` |
| 3.2 T7 | Control: VF0 uncapped (with VF0 `min-only`, the default since 2026-11-16, VF0 is already uncapped) | Remove the cap: `oc debug node/appworker-1… -- chroot /host ip link set ens1f0np0 vf 0 min_tx_rate 0 max_tx_rate 0`. Run `… T7_uncapped <out> -- -P 4 -t 20`. **Restore** with `ip link set ens1f0np0 vf 0 max_tx_rate 6250 min_tx_rate 6250` (`enforced`) or `max_tx_rate 0 min_tx_rate 6250` (`min-only`) |
| 3.3 | VF1 75 %, TRex → testpmd → TRex at 100 % | `M0VF=1 scripts/ngc809_e2e_cap_test.sh loop <out>`. This starts `/tmp/run_testpmd.sh` (256 descriptors) itself. For zero loss, run `run_testpmd_bigring.sh` and drive TRex with `oc -n dpdk exec trex -- bash -c 'cd /opt/trex; python3 /tmp/trex_e2e_run.py 1500 100% 10'` |
| 3.4 C1 | **Concurrent:** iperf3 aw1 → m0 + TRex | `tools/start_apps.sh`, then `scripts/ngc809_concurrent_split_test.sh C1 <out> -- -P 4` |
| 3.4 C2 | **Concurrent:** iperf3 both directions + TRex | `scripts/ngc809_concurrent_split_test.sh C2 <out> -- -P 4 --bidir` |
| — | Stop testpmd, clear PROMISC | `tools/start_apps.sh stop` |

Expected results (2026-11-11, both VFs `enforced`). Runs made with other rate modes are in their `runs/*/SUMMARY.txt`:
- **VF0 alone:** 6.22 Gbit/s L2. iperf3 reports about 5.97 Gbit/s of payload.
- **VF0 uncapped:** 24.54 Gbit/s.
- **VF1 alone:** 18.67 Gbit/s.
- **Concurrent:** VF0 6.20–6.22 and VF1 18.20–18.30 Gbit/s, PHY at 24.5–24.65 Gbit/s, i.e. **25.4 % : 74.6 %** on each loaded uplink.

---

## 6. Tear down (reverse order)

These commands are destructive, so review them before running:
```bash
oc -n dpdk delete pod iperf3-server iperf3-client trex testpmd
oc delete -f 04-sriovnetworks.yaml          # removes the NADs
oc delete -f 03-sriovnetworknodepolicies.yaml
oc delete -f 05-configmaps-trex.yaml
# NNCPs: deleting does NOT remove VFs or rates from the NIC. Set total-vfs/rates explicitly first if that is wanted.
oc delete -f 02-nncp-vf-partition.yaml
```

