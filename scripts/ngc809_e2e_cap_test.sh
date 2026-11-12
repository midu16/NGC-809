#!/usr/bin/env bash
# NGC-809: end-to-end proof that the SriovNetwork VF cap is enforced on the whole trex <-> testpmd path.
# Runs from the laptop through the hub. Per phase it samples, once per second on BOTH nodes, the VF and
# PHY TX/RX hardware counters (scripts/ngc809_vf_tx_sampler.sh) while traffic runs, then prints the
# steady-state rate at every NIC hop. Switch counters are not included (no switch credentials needed).
#   fwd  : TRex "100 %" -> master-0 VF0 kernel netdev (testpmd stopped)        = runbook P1
#   ret  : testpmd 25.11 txonly -> appworker-1 VF1 (TRex server receives)      = runbook P2
#   loop : TRex "100 %" -> testpmd mac-forward -> TRex                          = runbook P3
# Usage: [M0VF=<n>] scripts/ngc809_e2e_cap_test.sh <fwd|ret|loop> [outdir]
# M0VF = master-0 VF index used by testpmd (default 0; 1 since the 2026-11-11 move to VF1).
set -uo pipefail
cd "$(dirname "$0")/.."
PH=$1 OUT=${2:-/tmp} M0VF=${M0VF:-0}
K='export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml'
AW=appworker-1.cwl.integration.core.bos2.lab M0=master-0.cwl.integration.core.bos2.lab
S=$(base64 -w0 scripts/ngc809_vf_tx_sampler.sh)
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
hub() { ssh ngc-747-hub "$K; $1"; }
relearn() {  # one ARP from each kernel net1 so the switch knows both MACs (no unknown-unicast flood)
  hub "oc -n dpdk exec testpmd -- bash -c 'ip addr add 10.10.10.1/24 dev net1; echo x > /dev/udp/10.10.10.2/9; ip addr del 10.10.10.1/24 dev net1'; \
       oc -n dpdk exec trex -- bash -c 'ip addr add 10.10.10.2/24 dev net1; echo x > /dev/udp/10.10.10.1/9; ip addr del 10.10.10.2/24 dev net1'" >/dev/null 2>&1
}
TEL="echo /ethdev/stats,0 | dpdk-telemetry.py -f FP 2>/dev/null | tr -d \"\\n\" | grep -oE \"\\\"(ipackets|opackets|imissed|oerrors)\\\": [0-9]+\" | tr \"\\n\" \" \""
N=40
echo "$(ts) ==== phase $PH"
for n in $AW:1 $M0:$M0VF; do
  hub "oc debug -q node/${n%:*} -- chroot /host ip link show ens1f0np0 | grep vf" | sed "s/^ */$(ts) ${n%%.*} /"
done

case $PH in
  fwd)  relearn ;;
  loop) hub "oc -n dpdk exec testpmd -- bash -c 'nohup /tmp/run_testpmd.sh > /tmp/testpmd.log 2>&1 & disown'"
        echo "$(ts) testpmd mac-forward started (promisc off); 60 s for the 16:1 start-up block"; sleep 60; relearn
        echo "$(ts) testpmd before: $(hub "oc -n dpdk exec testpmd -- bash -c '${TEL/FP/sink}'")" ;;
  ret)  N=110; relearn ;;
esac

hub "oc debug -q node/$AW -- chroot /host bash -c 'echo $S | base64 -d | bash -s ens1f0np0 1 $N'" > "$OUT/${PH}_aw1.samples" 2>/dev/null & A=$!
hub "oc debug -q node/$M0 -- chroot /host bash -c 'echo $S | base64 -d | bash -s ens1f0np0 $M0VF $N'" > "$OUT/${PH}_m0.samples" 2>/dev/null & M=$!
sleep 12
case $PH in
  fwd|loop) echo "$(ts) trex start"
            echo "$(ts) trex: $(hub "oc -n dpdk exec trex -- bash -c 'cd /opt/trex; python3 /tmp/trex_e2e_run.py 1500 100% 10'" 2>/dev/null | tail -1)" ;;
  ret)      echo "$(ts) testpmd txonly start (10 s traffic after 75 s wait)"
            hub "oc -n dpdk exec testpmd -- /tmp/run_txonly.sh 10 75" > "$OUT/ret_txonly.log" 2>&1
            grep -E "TX-packets.*TX-errors" "$OUT/ret_txonly.log" | tail -1 | sed "s/^/$(ts) testpmd:/" ;;
esac
wait $A $M
if [ "$PH" = loop ]; then
  echo "$(ts) testpmd after:  $(hub "oc -n dpdk exec testpmd -- bash -c '${TEL/FP/sink}'")"
  hub "oc -n dpdk exec testpmd -- bash -c 'pkill -INT -f dpdk-testpmd; sleep 3; ip link set net1 promisc off'" >/dev/null 2>&1
fi
hub "for p in trex testpmd; do oc -n dpdk exec \$p -- ip link show net1 | head -1; done" | sed "s/^/$(ts) net1 after: /"

python3 - "$OUT/${PH}_aw1.samples" "$OUT/${PH}_m0.samples" "$M0VF" <<'EOF'
import sys, datetime
def rates(f):
    rows = [list(map(float, l.split())) for l in open(f) if len(l.split()) == 9]
    out = []
    for p, c in zip(rows, rows[1:]):
        dt = c[0] - p[0]; g = lambda i: (c[i] - p[i]) * 8 / dt / 1e9
        out.append(dict(t=c[0], vtx=g(1), vrx=g(3), ptx=g(5), prx=g(6), pause=(c[7]-p[7], c[8]-p[8])))
    return out
a, m = rates(sys.argv[1]), rates(sys.argv[2])
V = 'VF' + sys.argv[3]
print(f"time      | aw1 VF1 tx  PHY tx | m0 PHY rx  {V} rx | m0 {V} tx  PHY tx | aw1 PHY rx  VF1 rx | PAUSE aw1/m0")
for x, y in zip(a, m):
    if max(x['vtx'], x['vrx'], y['vtx'], y['vrx']) < 0.05: continue
    t = datetime.datetime.fromtimestamp(x['t'], datetime.timezone.utc).strftime('%H:%M:%S')
    print(f"{t}Z | {x['vtx']:6.2f} {x['ptx']:6.2f}   | {y['prx']:6.2f} {y['vrx']:6.2f}   | {y['vtx']:6.2f} {y['ptx']:6.2f}   | "
          f"{x['prx']:6.2f} {x['vrx']:6.2f}    | {x['pause']}/{y['pause']}")
def steady(rs, k):
    s = [r[k] for r in rs if r[k] > 1.0]
    s = s[1:-1] if len(s) > 2 else s
    return (sum(s) / len(s), max(s), len(s)) if s else (0, 0, 0)
print("steady state (mean / max Gbit/s, seconds):")
for name, rs, k in (("aw1 VF1 TX", a, 'vtx'), ("aw1 PHY TX", a, 'ptx'), ("m0 PHY RX", m, 'prx'), (f"m0 {V} RX", m, 'vrx'),
                    (f"m0 {V} TX", m, 'vtx'), ("m0 PHY TX", m, 'ptx'), ("aw1 PHY RX", a, 'prx'), ("aw1 VF1 RX", a, 'vrx')):
    mean, mx, n = steady(rs, k)
    if n: print(f"  {name:11s} {mean:6.2f} / {mx:6.2f}  ({n} s)")
EOF
