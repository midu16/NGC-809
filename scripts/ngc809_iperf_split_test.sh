#!/usr/bin/env bash
# NGC-809: prove the VF0 25 % (6250 Mbit/s) share with iperf3 over SR-IOV VF0 on master-0 and appworker-1.
# Runs from the laptop through the hub. Per test it samples, once per second on BOTH nodes, the VF0, VF1 and PHY
# hardware counters (scripts/ngc809_split_sampler.sh) while iperf3 runs between dpdk/iperf3-client (appworker-1,
# 10.10.30.2) and dpdk/iperf3-server (master-0, 10.10.30.1, `iperf3 -s`), then prints per-second and steady-state
# rates at every hop. Pods/networks: manifests/ngc809-iperf-vf0.yaml.
# Usage: scripts/ngc809_iperf_split_test.sh <name> <outdir> -- <iperf3 client args...>
#   e.g. scripts/ngc809_iperf_split_test.sh tcp_fwd_P4 /tmp/out -- -P 4 -t 20
set -uo pipefail
cd "$(dirname "$0")/.."
NAME=$1 OUT=$2; shift 3
K='export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml'
AW=appworker-1.cwl.integration.core.bos2.lab M0=master-0.cwl.integration.core.bos2.lab
S=$(base64 -w0 scripts/ngc809_split_sampler.sh)
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
hub() { ssh ngc-747-hub "$K; $1"; }
mkdir -p "$OUT"
N=32
echo "$(ts) ==== $NAME: iperf3 -c 10.10.30.1 -B 10.10.30.2 $*"
hub "oc debug -q node/$AW -- chroot /host bash -c 'echo $S | base64 -d | bash -s ens1f0np0 $N'" > "$OUT/${NAME}_aw1.samples" 2>/dev/null & A=$!
hub "oc debug -q node/$M0 -- chroot /host bash -c 'echo $S | base64 -d | bash -s ens1f0np0 $N'" > "$OUT/${NAME}_m0.samples" 2>/dev/null & M=$!
sleep 8
hub "oc -n dpdk exec iperf3-client -- iperf3 -c 10.10.30.1 -B 10.10.30.2 -f g $*" > "$OUT/${NAME}_iperf.txt" 2>&1
grep -E "sender|receiver|SUM.*(sender|receiver)|Jitter|%\)" "$OUT/${NAME}_iperf.txt" | tail -4 | sed "s/^/$(ts) iperf3: /"
wait $A $M
python3 - "$OUT/${NAME}_aw1.samples" "$OUT/${NAME}_m0.samples" <<'EOF' | tee "$OUT/${NAME}_rates.txt"
import sys, datetime
def rates(f):
    rows = [list(map(float, l.split())) for l in open(f) if len(l.split()) == 7]
    out = []
    for p, c in zip(rows, rows[1:]):
        dt = c[0] - p[0]; g = lambda i: (c[i] - p[i]) * 8 / dt / 1e9
        out.append(dict(t=c[0], v0tx=g(1), v0rx=g(2), v1tx=g(3), v1rx=g(4), ptx=g(5), prx=g(6)))
    return out
a, m = rates(sys.argv[1]), rates(sys.argv[2])
print("time      | aw1 VF0 tx  VF0 rx  VF1 tx  PHY tx  PHY rx | m0 VF0 tx  VF0 rx  VF1 tx  PHY tx  PHY rx   (Gbit/s L2)")
for x, y in zip(a, m):
    if max(x['v0tx'], y['v0tx'], x['v1tx'], y['v1tx']) < 0.05: continue
    t = datetime.datetime.fromtimestamp(x['t'], datetime.timezone.utc).strftime('%H:%M:%S')
    print(f"{t}Z |  {x['v0tx']:6.2f}  {x['v0rx']:6.2f}  {x['v1tx']:6.2f}  {x['ptx']:6.2f}  {x['prx']:6.2f} |"
          f"  {y['v0tx']:6.2f}  {y['v0rx']:6.2f}  {y['v1tx']:6.2f}  {y['ptx']:6.2f}  {y['prx']:6.2f}")
def steady(rs, k):
    s = [r[k] for r in rs if r[k] > 0.5]
    s = s[1:-1] if len(s) > 2 else s
    return (sum(s) / len(s), max(s), len(s)) if s else (0, 0, 0)
print("steady state (mean / max Gbit/s L2, seconds):")
for name, rs, k in (("aw1 VF0 TX", a, 'v0tx'), ("aw1 VF0 RX", a, 'v0rx'), ("aw1 VF1 TX", a, 'v1tx'), ("aw1 PHY TX", a, 'ptx'),
                    ("m0 VF0 RX", m, 'v0rx'), ("m0 VF0 TX", m, 'v0tx'), ("m0 VF1 TX", m, 'v1tx'), ("m0 PHY TX", m, 'ptx')):
    mean, mx, n = steady(rs, k)
    if n: print(f"  {name:11s} {mean:6.2f} / {mx:6.2f}  ({n} s)")
EOF
