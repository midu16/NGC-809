#!/usr/bin/env python3
"""Render an NGC-809 kustomize overlay and split it into the staged 01-07 manifests.

`kubectl kustomize <overlay>` outputs every object in one stream. tools/apply.sh has to apply
them in stages (NNCP -> SriovNetworkNodePolicy -> SriovNetwork -> ConfigMaps -> pods, with waits
in between; see README §4), so this writes one file per stage, using the same names as the root
manifests:
  01-namespace.yaml  02-nncp-vf-partition.yaml  03-sriovnetworknodepolicies.yaml
  04-sriovnetworks.yaml  05-configmaps-trex.yaml  06-pods-trex-testpmd.yaml  07-pods-iperf3.yaml

It exits non-zero if any "<placeholder>" is left in the rendered output.
With --check, it also compares every rendered object with the root 01-07 manifests
(parsed YAML, types included) and exits non-zero on any difference.

Rate modes (dynamic tx-rate render)
-----------------------------------
kustomize can set values but cannot delete fields, so render.py applies the VF tx-rate policy after
`kubectl kustomize`. Each VF has a mode, taken from site-values.yaml (spec.vf0.rateMode,
spec.vf1.rateMode; default "enforced"). You can override it with --vf0-mode/--vf1-mode, and the rate
with --vf0-rate/--vf1-rate:
  enforced  SriovNetwork minTxRate = maxTxRate = rate;  NNCP min-tx-rate = max-tx-rate = rate
  min-only  SriovNetwork minTxRate = rate, maxTxRate removed;  NNCP min-tx-rate: 0, max-tx-rate: 0
  removed   SriovNetwork minTxRate and maxTxRate removed;      NNCP min-tx-rate: 0, max-tx-rate: 0
The NNCP uses explicit 0s rather than missing keys: nmstate only changes the keys it is given.
In min-only, the guarantee comes from sriov-cni (netlink: min = rate, max 0) when the pod attaches.
NetworkManager cannot hold a min without a max: `max-tx-rate: 0` is rejected ("min_tx_rate is greater
than max_tx_rate"), and leaving max out fails verification ("min-tx-rate desire '6250', current '0'"). A SriovNetwork that has minTxRate but no maxTxRate makes
sriov-cni program max_tx_rate 0 (no cap) when a pod attaches. With neither field, sriov-cni leaves the
NNCP values alone.
render.py also writes expected-state.json (per node and VF: min/max rate, VLAN, trust and the NAD
rate keys). tools/rate_rollout.py checks the cluster and the hosts against it.

Usage (from ngc-809/):
  python3 tools/render.py kustomize/example-overlays-config/cwl                 # -> rendered/cwl/
  python3 tools/render.py kustomize/example-overlays-config/cwl --check
  python3 tools/render.py kustomize/example-overlays-config/<site> -o <dir>
  python3 tools/render.py kustomize/example-overlays-config/cwl --vf1-mode removed -o rendered/cwl-vf1-removed
  tools/apply.sh rendered/<site>                                                # staged apply
"""
import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGES = [  # (file, predicate on object)
    ("01-namespace.yaml", lambda o: o["kind"] == "Namespace"),
    ("02-nncp-vf-partition.yaml", lambda o: o["kind"] == "NodeNetworkConfigurationPolicy"),
    ("03-sriovnetworknodepolicies.yaml", lambda o: o["kind"] == "SriovNetworkNodePolicy"),
    ("04-sriovnetworks.yaml", lambda o: o["kind"] == "SriovNetwork"),
    ("05-configmaps-trex.yaml", lambda o: o["kind"] == "ConfigMap"),
    ("06-pods-trex-testpmd.yaml", lambda o: o["kind"] == "Pod" and o["metadata"]["name"] in ("trex", "testpmd")),
    ("07-pods-iperf3.yaml", lambda o: o["kind"] == "Pod"),
]
# "<namespace>", "<server-hostname>" ... ; does not match "pad_len < 0" in the TRex profile.
PLACEHOLDER = re.compile(r"<[a-z0-9][a-z0-9.-]*>")

RATE_MODES = ("enforced", "min-only", "removed")
# Fixed base object names (README §3.1): role -> NNCP, and VF index -> {role: SriovNetwork}.
NNCP = {"server": "ngc809-master0-nic2-vf-partition", "client": "ngc809-nic2-vf-partition"}
SRIOVNET = {0: {"server": "ngc809-vf0-25-m0", "client": "ngc809-vf0-25-aw1"},
            1: {"server": "ngc809-master0-sink", "client": "ngc809-tenant-75"}}


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def key(o):
    m = o["metadata"]
    return (o["kind"], m.get("namespace"), m["name"])


def rate_pair(mode, rate):
    """(min, max) that sriov-cni programs on the VF when a pod attaches; 0 = no limit."""
    return {"enforced": (rate, rate), "min-only": (rate, 0), "removed": (0, 0)}[mode]


def nncp_pair(mode, rate):
    """(min, max) in the NNCP: the host baseline when no pod is attached.
    min-only is 0/0 here. NetworkManager cannot hold a min without a max: `max-tx-rate: 0` is rejected
    ("min_tx_rate is greater than max_tx_rate"), and leaving max out fails verification
    ("min-tx-rate desire '6250', current '0'"; nmstate 2.2.60, CWL 2026-11-16)."""
    return (rate, rate) if mode == "enforced" else (0, 0)


def apply_rate_policy(objs, policy):
    """Edit the rendered NNCPs and SriovNetworks in place. policy = {vf: (mode, rate)}."""
    by = {(o["kind"], o["metadata"]["name"]): o for o in objs}
    for vf, (mode, rate) in policy.items():
        mn, mx = nncp_pair(mode, rate)
        for role, name in NNCP.items():
            for v in by[("NodeNetworkConfigurationPolicy", name)]["spec"]["desiredState"]["interfaces"][0]["ethernet"]["sr-iov"]["vfs"]:
                if v["id"] == vf:
                    v["max-tx-rate"], v["min-tx-rate"] = mx, mn
        for role, name in SRIOVNET[vf].items():
            spec = by[("SriovNetwork", name)]["spec"]
            spec.pop("maxTxRate", None)
            spec.pop("minTxRate", None)
            if mode in ("enforced", "min-only"):
                spec["minTxRate"] = rate
            if mode == "enforced":
                spec["maxTxRate"] = rate
            items = sorted(spec.items())  # same alphabetical key order as the root 04 manifest
            spec.clear()
            spec.update(items)


def expected_state(objs, policy):
    """Expected NAD keys and host `ip link` VF state once the pods are attached."""
    by = {(o["kind"], o["metadata"]["name"]): o for o in objs}
    pf = by[("NodeNetworkConfigurationPolicy", NNCP["server"])]["spec"]["desiredState"]["interfaces"][0]["name"]
    nodes = {r: by[("NodeNetworkConfigurationPolicy", n)]["spec"]["nodeSelector"]["kubernetes.io/hostname"]
             for r, n in NNCP.items()}
    ns = next(o for o in objs if o["kind"] == "Namespace")["metadata"]["name"]
    vfs = {}
    for vf, (mode, rate) in policy.items():
        mn, mx = rate_pair(mode, rate)
        sn = {r: by[("SriovNetwork", n)]["spec"] for r, n in SRIOVNET[vf].items()}
        vfs[str(vf)] = {
            "mode": mode, "rateMbps": rate, "min_tx_rate": mn, "max_tx_rate": mx,
            "nncp_min_tx_rate": nncp_pair(mode, rate)[0], "nncp_max_tx_rate": nncp_pair(mode, rate)[1],
            "networks": SRIOVNET[vf],
            "nad": {r: {k: v for k, v in (("min_tx_rate", s.get("minTxRate")), ("max_tx_rate", s.get("maxTxRate")))
                        if v is not None} for r, s in sn.items()},
            "vlan": {r: s.get("vlan", 0) for r, s in sn.items()},
            "trust": {r: s.get("trust", "off") == "on" for r, s in sn.items()},
        }
    return {"namespace": ns, "pf": pf, "nodes": nodes, "vfs": vfs}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("overlay", help="kustomize overlay directory")
    ap.add_argument("-o", "--out", help="output dir (default: rendered/<overlay-basename>)")
    ap.add_argument("--check", action="store_true", help="compare with the root 01-07 manifests")
    for vf in (0, 1):
        ap.add_argument(f"--vf{vf}-mode", choices=RATE_MODES, help=f"VF{vf} rate mode (default: site-values spec.vf{vf}.rateMode)")
        ap.add_argument(f"--vf{vf}-rate", type=int, help=f"VF{vf} rate in Mbit/s (default: site-values spec.vf{vf}.rateMbps)")
    a = ap.parse_args()

    sv_path = os.path.join(a.overlay, "site-values.yaml")
    sv = (yaml.safe_load(open(sv_path)) or {}).get("spec", {}) if os.path.exists(sv_path) else {}
    policy = {}
    for vf in (0, 1):
        mode = getattr(a, f"vf{vf}_mode") or sv.get(f"vf{vf}", {}).get("rateMode", "enforced")
        rate = getattr(a, f"vf{vf}_rate") or sv.get(f"vf{vf}", {}).get("rateMbps")
        if mode not in RATE_MODES:
            print(f"{ts()} ERROR spec.vf{vf}.rateMode={mode!r}; expected one of {RATE_MODES}", file=sys.stderr)
            return 1
        if mode != "removed" and (not isinstance(rate, int) or rate <= 0):
            print(f"{ts()} ERROR VF{vf} mode {mode} needs a positive integer rate (got {rate!r})", file=sys.stderr)
            return 1
        policy[vf] = (mode, rate or 0)

    out = a.out or os.path.join(ROOT, "rendered", os.path.basename(os.path.normpath(a.overlay)))
    text = subprocess.run(["kubectl", "kustomize", a.overlay], check=True, capture_output=True, text=True).stdout
    objs = [o for o in yaml.safe_load_all(text) if o]

    left = sorted(set(PLACEHOLDER.findall(text)))
    if left:
        print(f"{ts()} ERROR unresolved placeholders in {a.overlay}: {' '.join(left)}", file=sys.stderr)
        return 1

    apply_rate_policy(objs, policy)
    for vf, (mode, rate) in policy.items():
        mn, mx = rate_pair(mode, rate)
        print(f"{ts()} VF{vf} rate mode {mode}: min_tx_rate {mn} max_tx_rate {mx} Mbit/s (0 = no limit)")

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "expected-state.json"), "w") as f:
        json.dump(expected_state(objs, policy), f, indent=1)
    rest = list(objs)
    for fname, pred in STAGES:
        sel = [o for o in rest if pred(o)]
        rest = [o for o in rest if not pred(o)]
        with open(os.path.join(out, fname), "w") as f:
            f.write(f"# NGC-809 — rendered from {os.path.relpath(a.overlay, ROOT)} by tools/render.py. Do not edit.\n")
            f.write("# rate modes: " + ", ".join(f"VF{v} {m} {r}" for v, (m, r) in policy.items()) + "\n")
            for o in sel:
                f.write("---\n" + yaml.safe_dump(o, sort_keys=False, width=120))
        print(f"{ts()} wrote {os.path.relpath(os.path.join(out, fname), ROOT)} ({len(sel)} objects)")
    if rest:
        print(f"{ts()} ERROR objects with no stage: {[key(o) for o in rest]}", file=sys.stderr)
        return 1

    if a.check:
        live = {}
        for p in sorted(glob.glob(os.path.join(ROOT, "0[1-7]-*.yaml"))):
            for o in yaml.safe_load_all(open(p)):
                if o:
                    live[key(o)] = o
        rend = {key(o): o for o in objs}
        bad = 0
        for k in sorted(set(live) | set(rend), key=str):
            if live.get(k) != rend.get(k):
                bad += 1
                print(f"{ts()} DIFF {k}")
                sys.stdout.writelines(difflib.unified_diff(
                    json.dumps(live.get(k), indent=1, sort_keys=True).splitlines(True),
                    json.dumps(rend.get(k), indent=1, sort_keys=True).splitlines(True), "live", "rendered", n=1))
        print(f"{ts()} check: {len(rend) - bad}/{len(set(live) | set(rend))} objects identical to the root 01-07 manifests")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
