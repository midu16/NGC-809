#!/usr/bin/env python3
"""NGC-809: roll a VF tx-rate profile out to CWL, check it on OpenShift and on the nodes, re-deploy the
workloads and run the README §5 tests.

Runs from the laptop. Every `oc` call goes through `ssh ngc-747-hub` with
KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml.

Steps (`all` runs all of them in order; each one is also its own subcommand):
  render   tools/render.py <overlay> [--vf0-mode/--vf1-mode/--vf0-rate/--vf1-rate] -> rendered/<name>/
           (01-07 + expected-state.json). Modes are enforced | min-only | removed, see tools/render.py.
  plan     `oc diff` of the rendered NNCPs (02), SriovNetworks (04) and SriovNetworkNodePolicies (03)
           against the cluster. Changes nothing.
  apply    1. preflight: the 03 SriovNetworkNodePolicies must already match the cluster (README pitfall 1:
              changing them rebinds VFs). Both SriovNetworkNodeStates must be Succeeded.
           2. delete the trex/testpmd/iperf3 pods if they exist. A running pod keeps the rates sriov-cni
              programmed when it attached, and a re-applied NNCP resets its VLAN (pitfalls 2 and 3).
           3. apply 01 + 02 and wait until every NNCE is Available for the new NNCP generation.
           4. check the NNCP specs, the NodeNetworkState and `ip link` on both hosts against the NNCP rates.
           5. apply 04 and wait until the 4 NADs contain exactly the expected min_tx_rate/max_tx_rate keys.
           6. wait for the 4 VF resources to be allocatable, apply 05, 06 and 07, and wait until the pods are Ready.
           7. tools/restore_pod_files.sh. If the trex pod received other CPUs or another VF than
              trex_cfg.yaml expects, write a corrected /etc/trex_cfg.yaml into the pod.
  verify   CR level (SriovNetwork spec, NAD config, NNCP spec, NNS) and host level (`ip link show <pf>`:
           max_tx_rate / min_tx_rate / vlan / trust of VF0 and VF1 on both nodes, compared with
           expected-state.json; once the pods are attached, sriov-cni's values are the ones expected).
  test     tools/start_apps.sh and the README §5 drivers. Output goes to <out>/:
             iperf  T1-T6  scripts/ngc809_iperf_split_test.sh       (VF0 alone, before the DPDK apps start)
             conc   C1, C2 scripts/ngc809_concurrent_split_test.sh   (after tools/start_apps.sh)
             vf1    3.3    M0VF=1 scripts/ngc809_e2e_cap_test.sh loop (testpmd stopped first: the loop starts its own)
           Each driver's output is tee'd to <out>/<test>.log. A summary is written to <out>/SUMMARY.txt.

Usage (from ngc-809/):
  tools/rate_rollout.py all                                   # site-values modes (CWL: VF1 enforced, VF0 min-only)
  tools/rate_rollout.py all --vf1-mode removed                # same, with the VF1 limits removed
  tools/rate_rollout.py plan --vf0-mode enforced              # what would change
  tools/rate_rollout.py verify                                # check the current cluster against the render
  tools/rate_rollout.py test --tests iperf,conc --out runs/x  # tests only
Options: --overlay DIR (default kustomize/example-overlays-config/cwl), --name NAME (rendered/<NAME>,
default derived from the modes), --out DIR (default runs/<UTC timestamp>-<name>), --yes (do not ask
before deleting the pods).
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KCFG = "export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml"
SRIOV_NS = "openshift-sriov-network-operator"
PODS = ("trex", "testpmd", "iperf3-server", "iperf3-client")
# kubelet resources per role (fixed in the base, README §3.1)
RESOURCES = {"server": ("ngc809_srv_nic2_vf", "ngc809_m0_vf0"), "client": ("ngc809_nic2_vf1", "ngc809_aw1_vf0")}
NNCP = {"server": "ngc809-master0-nic2-vf-partition", "client": "ngc809-nic2-vf-partition"}
TRex_CFG = os.path.join(ROOT, "pod-files/trex/etc/trex_cfg.yaml")


class Fail(Exception):
    pass


class Fatal(Fail):
    """Stops a wait() immediately (e.g. NNCE Failing)."""


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg):
    print(f"{ts()} {msg}")


def hub(cmd, stdin=None, check=True):
    r = subprocess.run(["ssh", "ngc-747-hub", f"{KCFG}; {cmd}"], input=stdin, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Fail(f"hub command failed ({r.returncode}): {cmd}\n{r.stderr.strip()}")
    return r


def oc_json(args):
    return json.loads(hub(f"oc {args} -o json").stdout)


def wait(desc, fn, tries=60, delay=8):
    last = None
    for _ in range(tries):
        try:
            ok, last = fn()
        except Fatal:
            raise
        except Fail as e:
            ok, last = False, str(e)
        if ok:
            log(f"ok: {desc}")
            return
        time.sleep(delay)
    raise Fail(f"TIMEOUT: {desc} (last: {last})")


# ------------------------------------------------------------------ render
def mode_args(a):
    out = []
    for vf in (0, 1):
        if getattr(a, f"vf{vf}_mode"):
            out += [f"--vf{vf}-mode", getattr(a, f"vf{vf}_mode")]
        if getattr(a, f"vf{vf}_rate"):
            out += [f"--vf{vf}-rate", str(getattr(a, f"vf{vf}_rate"))]
    return out


def render(a):
    cmd = [sys.executable, os.path.join(ROOT, "tools/render.py"), a.overlay, "-o", a.dir] + mode_args(a)
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write("".join(l + "\n" for l in r.stdout.splitlines() if "rate mode" in l or "ERROR" in l))
    if r.returncode != 0:
        raise Fail(f"render failed:\n{r.stderr}")
    a.exp = json.load(open(os.path.join(a.dir, "expected-state.json")))
    log(f"rendered {os.path.relpath(a.dir, ROOT)}/")


def f(a, name):
    return open(os.path.join(a.dir, name)).read()


def apply_file(a, name):
    r = hub("oc apply -f -", stdin=f(a, name), check=False)
    for l in (r.stdout + r.stderr).splitlines():
        if l and not l.startswith("Warning: would violate PodSecurity"):
            log(f"  {l}")
    if r.returncode != 0:
        raise Fail(f"oc apply {name} failed")


def diff_file(a, name):
    r = hub("oc diff -f -", stdin=f(a, name), check=False)
    if r.returncode > 1:
        raise Fail(f"oc diff {name} failed: {r.stderr.strip()}")
    return r.returncode == 1, r.stdout


def nncp_settled():
    """Same rule as the cluster's nncp-validation-policy VAP: a spec UPDATE is only admitted once the NNCP's
    Available condition has a non-empty reason other than ConfigurationProgressing."""
    def fn():
        st = {}
        for name in NNCP.values():
            r = hub(f"oc get nncp {name} --ignore-not-found -o json", check=False).stdout
            if not r.strip():
                st[name] = "absent"
                continue
            av = next((c for c in json.loads(r).get("status", {}).get("conditions", []) if c["type"] == "Available"), {})
            st[name] = av.get("reason") or "no-reason"
        return all(v not in ("", "no-reason", "ConfigurationProgressing") for v in st.values()), st
    return fn


# ------------------------------------------------------------------ plan
def plan(a):
    wait("NNCPs settled (nncp-validation-policy rejects updates while in progress)", nncp_settled(), tries=90, delay=10)
    for name in ("02-nncp-vf-partition.yaml", "03-sriovnetworknodepolicies.yaml", "04-sriovnetworks.yaml"):
        changed, out = diff_file(a, name)
        log(f"{name}: {'CHANGES' if changed else 'in sync'}")
        if changed:
            print("\n".join("    " + l for l in out.splitlines() if l[:1] in "+-@" and not l.startswith(("+++", "---"))
                            and "generation" not in l and "managedFields" not in l))


# ------------------------------------------------------------------ verify
VF_RE = re.compile(r"^\s*vf (\d+)\s+(.*)$")


def host_vfs(node, pf):
    out = hub(f"oc debug -q node/{node} -- chroot /host ip link show {pf}").stdout
    vfs = {}
    for line in out.splitlines():
        m = VF_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        g = lambda rx: (lambda x: int(x.group(1)) if x else 0)(re.search(rx, rest))
        vfs[m.group(1)] = {"max_tx_rate": g(r"max_tx_rate (\d+)Mbps"), "min_tx_rate": g(r"min_tx_rate (\d+)Mbps"),
                           "vlan": g(r"vlan (\d+)"), "trust": " trust on" in rest, "raw": line.strip()}
    return vfs


def nncp_rates(nncp):
    return {str(v["id"]): (v.get("min-tx-rate"), v.get("max-tx-rate"))
            for v in nncp["spec"]["desiredState"]["interfaces"][0]["ethernet"]["sr-iov"]["vfs"]}


def verify_cr(a):
    """CR level. Returns a list of mismatch strings."""
    exp, bad = a.exp, []
    ns = exp["namespace"]
    for vf, e in exp["vfs"].items():
        for role, net in e["networks"].items():
            sn = oc_json(f"-n {SRIOV_NS} get sriovnetwork {net}")["spec"]
            got = {k: sn[s] for k, s in (("min_tx_rate", "minTxRate"), ("max_tx_rate", "maxTxRate")) if s in sn}
            nad = json.loads(oc_json(f"-n {ns} get net-attach-def {net}")["spec"]["config"])
            got_nad = {k: nad[k] for k in ("min_tx_rate", "max_tx_rate") if k in nad}
            want = e["nad"][role]
            st = "ok" if got == want == got_nad else "MISMATCH"
            log(f"  VF{vf} {role:6s} SriovNetwork {net:20s} spec={got} NAD={got_nad} want={want}  {st}")
            if st != "ok":
                bad.append(f"VF{vf} {net}: spec {got} NAD {got_nad} want {want}")
    for role, name in NNCP.items():
        nncp = oc_json(f"get nncp {name}")
        node = exp["nodes"][role]
        nns = oc_json(f"get nns {node}")
        cur = next(i for i in nns["status"]["currentState"]["interfaces"] if i["name"] == exp["pf"])
        cur = {str(v["id"]): (v.get("min-tx-rate", 0), v.get("max-tx-rate", 0)) for v in cur["ethernet"]["sr-iov"]["vfs"]}
        spec = nncp_rates(nncp)
        for vf, e in exp["vfs"].items():
            want = (e["nncp_min_tx_rate"], e["nncp_max_tx_rate"])
            st = "ok" if spec.get(vf) == want else "MISMATCH"
            log(f"  VF{vf} {role:6s} NNCP {name} (min,max)={spec.get(vf)} want={want}  {st};  NNS current={cur.get(vf)}")
            if st != "ok":
                bad.append(f"NNCP {name} VF{vf}: {spec.get(vf)} want {want}")
    return bad


def verify_host(a, attached):
    """Host level `ip link`. attached=True: expect sriov-cni values (pods running); else the NNCP values."""
    exp, bad = a.exp, []
    for role, node in exp["nodes"].items():
        vfs = host_vfs(node, exp["pf"])
        for vf, e in exp["vfs"].items():
            h = vfs.get(vf)
            if h is None:
                bad.append(f"{node} VF{vf} missing in ip link")
                continue
            if attached:
                want = {"min_tx_rate": e["min_tx_rate"], "max_tx_rate": e["max_tx_rate"],
                        "vlan": e["vlan"][role], "trust": e["trust"][role]}
            else:
                want = {"min_tx_rate": e["nncp_min_tx_rate"], "max_tx_rate": e["nncp_max_tx_rate"]}
            got = {k: h[k] for k in want}
            st = "ok" if got == want else "MISMATCH"
            log(f"  {node.split('.')[0]:11s} {h['raw']}")
            if st != "ok":
                log(f"    -> MISMATCH want {want}")
                bad.append(f"{node} VF{vf}: got {got} want {want}")
    return bad


def pods_running(a):
    r = hub(f"oc -n {a.exp['namespace']} get pod {' '.join(PODS)} --no-headers -o custom-columns=P:.status.phase",
            check=False)
    return r.returncode == 0 and r.stdout.split() == ["Running"] * len(PODS)


def verify(a):
    log("verify: OpenShift objects")
    bad = verify_cr(a)
    attached = pods_running(a)
    log(f"verify: hosts ({'pods attached: sriov-cni values' if attached else 'no pods: NNCP values'})")
    bad += verify_host(a, attached)
    if bad:
        raise Fail("verification failed:\n  " + "\n  ".join(bad))
    log("verify: all rate limits match the render")


# ------------------------------------------------------------------ apply
def nnce_ok(exp):
    def fn():
        out = []
        for role, name in NNCP.items():
            gen = oc_json(f"get nncp {name}")["metadata"]["generation"]
            nnce = oc_json(f"get nnce {exp['nodes'][role]}.{name}")
            st = {c["type"]: c["status"] for c in nnce["status"].get("conditions", [])}
            out.append((name, gen, nnce["status"].get("policyGeneration"), st.get("Available"), st.get("Failing")))
            if st.get("Failing") == "True":
                raise Fatal(f"NNCE {name} on {exp['nodes'][role]} is Failing: "
                                 f"{[c.get('message') for c in nnce['status']['conditions'] if c['type'] == 'Failing']}")
        return all(g == pg and av == "True" for _, g, pg, av, _ in out), out
    return fn


def nads_ok(exp):
    def fn():
        got = {}
        for vf, e in exp["vfs"].items():
            for role, net in e["networks"].items():
                nad = json.loads(oc_json(f"-n {exp['namespace']} get net-attach-def {net}")["spec"]["config"])
                got[net] = ({k: nad[k] for k in ("min_tx_rate", "max_tx_rate") if k in nad}, e["nad"][role])
        return all(g == w for g, w in got.values()), got
    return fn


def resources_ok(exp):
    def fn():
        vals = []
        for role, res in RESOURCES.items():
            alloc = oc_json(f"get node {exp['nodes'][role]}")["status"]["allocatable"]
            vals += [alloc.get(f"openshift.io/{r}", "0") for r in res]
        return vals == ["1"] * 4, vals
    return fn


def fix_trex_cfg(a):
    ns = a.exp["namespace"]
    got = hub(f"oc -n {ns} exec trex -- sh -c 'grep Cpus_allowed_list /proc/1/status | cut -f2; "
              f"echo $PCIDEVICE_OPENSHIFT_IO_NGC809_NIC2_VF1'").stdout.split()
    if len(got) != 2:
        raise Fail(f"cannot read trex CPUs/VF: {got}")
    cpus = []
    for part in got[0].split(","):
        lo, _, hi = part.partition("-")
        cpus += list(range(int(lo), int(hi or lo) + 1))
    pci = got[1]
    cfg = open(TRex_CFG).read()
    want = (f"master_thread_id: {cpus[0]}" in cfg and f"latency_thread_id: {cpus[1]}" in cfg
            and f"threads: [{', '.join(map(str, cpus[2:]))}]" in cfg and f'"{pci}"' in cfg) if len(cpus) >= 4 else False
    if want:
        log(f"trex_cfg.yaml matches the pod (cpus {got[0]}, VF {pci})")
        return
    if len(cpus) < 4:
        raise Fail(f"trex pod got {len(cpus)} CPUs ({got[0]}); trex_cfg.yaml needs >= 4")
    cfg = re.sub(r'interfaces:\n(\s+)- "[0-9a-f:.]+"', lambda m: f'interfaces:\n{m.group(1)}- "{pci}"', cfg)
    cfg = re.sub(r"master_thread_id: \d+", f"master_thread_id: {cpus[0]}", cfg)
    cfg = re.sub(r"latency_thread_id: \d+", f"latency_thread_id: {cpus[1]}", cfg)
    cfg = re.sub(r"threads: \[[0-9, ]+\]", f"threads: [{', '.join(map(str, cpus[2:]))}]", cfg)
    hub(f"oc -n {ns} exec -i trex -- sh -c 'cat > /etc/trex_cfg.yaml'", stdin=cfg)
    log(f"trex pod got cpus {got[0]} VF {pci}: wrote an adjusted /etc/trex_cfg.yaml into the pod "
        f"(master {cpus[0]}, latency {cpus[1]}, dp {cpus[2:]}); pod-files/ left unchanged")


def apply(a):
    exp, ns = a.exp, a.exp["namespace"]
    # 1. preflight
    changed, out = diff_file(a, "03-sriovnetworknodepolicies.yaml")
    if changed:
        raise Fail("03 SriovNetworkNodePolicies differ from the cluster. This tool does not change them "
                   "(README pitfall 1). Apply them with tools/apply.sh first.\n" + out)
    for role, node in exp["nodes"].items():
        s = oc_json(f"-n {SRIOV_NS} get sriovnetworknodestate {node}")["status"].get("syncStatus")
        if s != "Succeeded":
            raise Fail(f"SriovNetworkNodeState {node} is {s}, not Succeeded")
    log("preflight: SriovNetworkNodePolicies in sync, node states Succeeded")

    # 2. pods
    present = hub(f"oc -n {ns} get pod {' '.join(PODS)} --ignore-not-found -o name", check=False).stdout.split()
    if present:
        if not a.yes:
            ans = input(f"{ts()} delete {' '.join(present)} in {ns} so that they re-attach with the new rates? [y/N] ")
            if ans.strip().lower() != "y":
                raise Fail("aborted by user")
        log(f"deleting {' '.join(present)}")
        hub(f"oc -n {ns} delete {' '.join(present)} --wait=true --timeout=180s")

    # 3. NNCP
    apply_file(a, "01-namespace.yaml")
    apply_file(a, "02-nncp-vf-partition.yaml")
    time.sleep(10)
    wait("NNCEs Available at the current NNCP generation", nnce_ok(exp), tries=45)

    # 4. NNCP + host (no pods yet)
    log("check: NNCP / NNS / hosts (no pods attached)")
    bad = verify_host(a, attached=False)
    if bad:
        raise Fail("host VF rates do not match the NNCP:\n  " + "\n  ".join(bad))

    # 5. SriovNetworks -> NADs
    apply_file(a, "04-sriovnetworks.yaml")
    wait("NADs carry the expected min_tx_rate/max_tx_rate", nads_ok(exp), tries=30, delay=5)

    # 6. pods
    wait("4 VF resources allocatable", resources_ok(exp), tries=40)
    for name in ("05-configmaps-trex.yaml", "06-pods-trex-testpmd.yaml", "07-pods-iperf3.yaml"):
        apply_file(a, name)
    log("waiting for pods Ready")
    hub(f"oc -n {ns} wait {' '.join('pod/' + p for p in PODS)} --for=condition=Ready --timeout=300s")

    # 7. in-pod files
    r = subprocess.run([os.path.join(ROOT, "tools/restore_pod_files.sh")], capture_output=True, text=True)
    sys.stdout.write("".join(f"{ts()}   {l}\n" for l in (r.stdout + r.stderr).splitlines() if "WARNING" not in l))
    if r.returncode != 0:
        raise Fail("tools/restore_pod_files.sh failed")
    fix_trex_cfg(a)


# ------------------------------------------------------------------ test
IPERF = [("T1_tcp_fwd_P4", "-P 4 -t 20"), ("T2_tcp_rev_P4", "-P 4 -t 20 -R"), ("T3_tcp_bidir_P4", "-P 4 -t 20 --bidir"),
         ("T4_udp_fwd_12G", "-u -b 3G -P 4 -l 1448 -t 20"), ("T5_tcp_fwd_P1", "-P 1 -t 20"), ("T6_tcp_fwd_P8", "-P 8 -t 20")]
CONC = [("C1", "-P 4"), ("C2", "-P 4 --bidir")]


def run(out, name, argv, env=None):
    log(f"---- {name}: {' '.join(argv)}")
    path = os.path.join(out, f"{name}.log")
    with open(path, "w") as fh:
        p = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env={**os.environ, **(env or {})})
        for line in p.stdout:
            sys.stdout.write(line)
            fh.write(line)
        rc = p.wait()
    if rc != 0:
        log(f"WARNING {name} exited {rc}")
    return path


def steady_lines(path):
    keep, out = False, []
    for l in open(path):
        if l.startswith("steady state"):
            keep = True
        if keep and l.strip():
            out.append(l.rstrip())
    return out


def test(a):
    out = a.out
    os.makedirs(out, exist_ok=True)
    tests = set(a.tests.split(","))
    logs = []
    with open(os.path.join(out, "expected-state.json"), "w") as fh:
        json.dump(a.exp, fh, indent=1)
    if "iperf" in tests:
        hub(f"oc -n {a.exp['namespace']} exec testpmd -- pkill -INT -x dpdk-testpmd", check=False)
        for name, args in IPERF:
            logs.append(run(out, name, ["scripts/ngc809_iperf_split_test.sh", name, out, "--"] + args.split()))
    if tests & {"conc", "vf1"}:
        logs.append(run(out, "start_apps", ["tools/start_apps.sh"]))
    if "conc" in tests:
        for name, args in CONC:
            logs.append(run(out, name, ["scripts/ngc809_concurrent_split_test.sh", name, out, "--"] + args.split()))
    if "vf1" in tests:
        run(out, "stop_testpmd", ["tools/start_apps.sh", "stop"])
        logs.append(run(out, "VF1_e2e_loop", ["scripts/ngc809_e2e_cap_test.sh", "loop", out], env={"M0VF": "1"}))
    with open(os.path.join(out, "SUMMARY.txt"), "w") as fh:
        fh.write(f"NGC-809 rate rollout test run {ts()}\n")
        for vf, e in a.exp["vfs"].items():
            fh.write(f"VF{vf}: mode {e['mode']}, min_tx_rate {e['min_tx_rate']}, max_tx_rate {e['max_tx_rate']} Mbit/s (0 = none)\n")
        for p in logs:
            fh.write(f"\n== {os.path.basename(p)}\n" + "\n".join(steady_lines(p)) + "\n")
    log(f"summary: {os.path.relpath(os.path.join(out, 'SUMMARY.txt'), ROOT)}")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=("render", "plan", "apply", "verify", "test", "all"))
    ap.add_argument("--overlay", default=os.path.join(ROOT, "kustomize/example-overlays-config/cwl"))
    ap.add_argument("--name", help="rendered/<name> (default: <overlay>[-vf0-<mode>][-vf1-<mode>])")
    for vf in (0, 1):
        ap.add_argument(f"--vf{vf}-mode", choices=("enforced", "min-only", "removed"))
        ap.add_argument(f"--vf{vf}-rate", type=int)
    ap.add_argument("--tests", default="iperf,conc,vf1", help="comma list of iperf,conc,vf1 (default: all)")
    ap.add_argument("--out", help="test output dir (default: runs/<timestamp>-<name>)")
    ap.add_argument("--yes", action="store_true", help="delete existing test pods without asking")
    a = ap.parse_args()
    a.overlay = os.path.abspath(a.overlay)
    suffix = "".join(f"-vf{vf}-{getattr(a, f'vf{vf}_mode')}" for vf in (0, 1) if getattr(a, f"vf{vf}_mode"))
    a.name = a.name or os.path.basename(os.path.normpath(a.overlay)) + suffix
    a.dir = os.path.join(ROOT, "rendered", a.name)
    a.out = os.path.abspath(a.out or os.path.join(ROOT, "runs", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + a.name))
    try:
        render(a)
        if a.step == "plan":
            plan(a)
        if a.step in ("apply", "all"):
            plan(a)
            apply(a)
        if a.step in ("verify", "apply", "all"):
            verify(a)
        if a.step in ("test", "all"):
            test(a)
    except Fail as e:
        log(f"FAILED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
