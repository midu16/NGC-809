#!/usr/bin/env bash
# NGC-809: concurrent proof of the 25 % / 75 % split on ens1f0np0 (master-0 <-> appworker-1).
# VF0 (6250): iperf3 dpdk/iperf3-client (appworker-1, 10.10.30.2) -> dpdk/iperf3-server (master-0, 10.10.30.1).
# VF1 (18750): TRex dpdk/trex (appworker-1) "100 %" 1500 B -> testpmd mac-forward (master-0) -> TRex.
# Runs from the laptop through the hub. Prerequisites: TRex server up (t-rex-64 -i), testpmd running
# (/tmp/run_testpmd_bigring.sh, promisc off), iperf3 pods Running (manifests/ngc809-iperf-vf0.yaml).
# Timeline: samplers start (once per second, both nodes, VF0/VF1/PHY HW counters via
# scripts/ngc809_split_sampler.sh) -> iperf3 starts (-t IPERF_T) -> after LEAD s TRex starts (1500 B, 100 %,
# TREX_D s nominal) -> both overlap -> TRex ends -> iperf3 ends. Prints per-second rates, per-phase steady
# state (iperf-only vs concurrent) and the VF0:VF1 share of each node's egress.
# Usage: scripts/ngc809_concurrent_split_test.sh <name> <outdir> -- <extra iperf3 client args>
set -uo pipefail
cd "$(dirname "$0")/.."
NAME=$1 OUT=$2; shift 3
IPERF_T=${IPERF_T:-40} LEAD=${LEAD:-8} TREX_D=${TREX_D:-15} N=${N:-56}
K='export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml'
AW=appworker-1.cwl.integration.core.bos2.lab M0=master-0.cwl.integration.core.bos2.lab
S=$(base64 -w0 scripts/ngc809_split_sampler.sh)
TEL="echo /ethdev/stats,0 | dpdk-telemetry.py -f sink 2>/dev/null | tr -d \"\\n\" | grep -oE \"\\\"(ipackets|opackets|imissed|oerrors)\\\": [0-9]+\" | tr \"\\n\" \" \""
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
hub() { ssh ngc-747-hub "$K; $1"; }
mkdir -p "$OUT"
echo "$(ts) ==== $NAME  iperf3 -t $IPERF_T $*  |  TRex 1500B 100% ${TREX_D}s starts after ${LEAD}s"
for n in $AW $M0; do hub "oc debug -q node/$n -- chroot /host ip link show ens1f0np0 | grep 'vf '" | sed "s/^ */$(ts) ${n%%.*} /"; done
# re-learn both DPDK MACs on the switch (MAC aging 300 s; testpmd only sends after it receives)
hub "oc -n dpdk exec testpmd -- bash -c 'ip addr add 10.10.10.1/24 dev net1; echo x > /dev/udp/10.10.10.2/9; ip addr del 10.10.10.1/24 dev net1'; \
     oc -n dpdk exec trex -- bash -c 'ip addr add 10.10.10.2/24 dev net1; echo x > /dev/udp/10.10.10.1/9; ip addr del 10.10.10.2/24 dev net1'" >/dev/null 2>&1
echo "$(ts) testpmd before: $(hub "oc -n dpdk exec testpmd -- bash -c '$TEL'")"
hub "oc debug -q node/$AW -- chroot /host bash -c 'echo $S | base64 -d | bash -s ens1f0np0 $N'" > "$OUT/${NAME}_aw1.samples" 2>/dev/null & A=$!
hub "oc debug -q node/$M0 -- chroot /host bash -c 'echo $S | base64 -d | bash -s ens1f0np0 $N'" > "$OUT/${NAME}_m0.samples" 2>/dev/null & M=$!
sleep 6
echo "$(ts) iperf3 start"
hub "oc -n dpdk exec iperf3-client -- iperf3 -c 10.10.30.1 -B 10.10.30.2 -f g -t $IPERF_T $*" > "$OUT/${NAME}_iperf.txt" 2>&1 & I=$!
sleep "$LEAD"
echo "$(ts) TRex start"
hub "oc -n dpdk exec trex -- bash -c 'cd /opt/trex; python3 /tmp/trex_e2e_run.py 1500 100% $TREX_D'" 2>/dev/null | tail -1 > "$OUT/${NAME}_trex.json"
echo "$(ts) TRex done: $(cat "$OUT/${NAME}_trex.json")"
wait $I
echo "$(ts) iperf3 done:"; grep -E "SUM.*(sender|receiver)|^\[ *5\].*(sender|receiver)" "$OUT/${NAME}_iperf.txt" | tail -4 | sed 's/^/    /'
wait $A $M
echo "$(ts) testpmd after:  $(hub "oc -n dpdk exec testpmd -- bash -c '$TEL'")"
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
print("time      | appworker-1 egress: VF0 tx  VF1 tx  sum   PHY tx | master-0 egress: VF0 tx  VF1 tx  sum   PHY tx | phase")
rows = []
for x, y in zip(a, m):
    if max(x['v0tx'], y['v0tx'], x['v1tx'], y['v1tx']) < 0.05: continue
    ph = "both" if x['v0tx'] > 1 and x['v1tx'] > 1 else ("VF0 only" if x['v0tx'] > 1 else ("VF1 only" if x['v1tx'] > 1 else "-"))
    rows.append((ph, x, y))
    t = datetime.datetime.fromtimestamp(x['t'], datetime.timezone.utc).strftime('%H:%M:%S')
    print(f"{t}Z |  {x['v0tx']:6.2f}  {x['v1tx']:6.2f}  {x['v0tx']+x['v1tx']:6.2f}  {x['ptx']:6.2f}   |"
          f"  {y['v0tx']:6.2f}  {y['v1tx']:6.2f}  {y['v0tx']+y['v1tx']:6.2f}  {y['ptx']:6.2f}   | {ph}")
def stat(ph, f):
    s = [f(x, y) for p, x, y in rows if p == ph]
    s = s[1:-1] if len(s) > 2 else s
    return (sum(s) / len(s), len(s)) if s else (0.0, 0)
for ph in ("VF0 only", "both", "VF1 only"):
    n = stat(ph, lambda x, y: 0)[1]
    if not n: continue
    print(f"steady state, phase '{ph}' ({n} s, Gbit/s L2 mean):")
    for node, k in (("appworker-1", 0), ("master-0", 1)):
        g = lambda key: stat(ph, lambda x, y, key=key: (x, y)[k][key])[0]
        v0, v1, p = g('v0tx'), g('v1tx'), g('ptx')
        share = f"  share VF0:VF1 = {100*v0/(v0+v1):4.1f} % : {100*v1/(v0+v1):4.1f} %" if v0 + v1 > 0 else ""
        print(f"  {node:11s} egress  VF0 {v0:6.2f}  VF1 {v1:6.2f}  sum {v0+v1:6.2f}  PHY {p:6.2f}{share}")
EOF
