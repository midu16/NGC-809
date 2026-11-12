#!/usr/bin/env bash
# NGC-809: start the DPDK applications used by the tests.
#   trex:    TRex 2.87 stateless server  `t-rex-64 -i --cfg /etc/trex_cfg.yaml` (ports 4500/4501)
#   testpmd: dpdk-testpmd 25.11 mac-forward, promiscuous/allmulti OFF, 4096-descriptor rings (/tmp/run_testpmd_bigring.sh;
#            auto-quits after ~28 min). Promisc must stay off: trusted VF + promisc -> STP loopback block on leaf_a.
# Then re-learns both DPDK MACs on the switch (one ARP from each kernel net1) and runs a 64 B sanity run.
# `stop` stops testpmd and clears net1 PROMISC (TRex server is left running).
# Process checks use `pgrep -x <comm>`: `pgrep -f <pattern>` inside `bash -c '...<pattern>...'` matches that bash
# itself, so the "already running" guard was always true and nothing was started.
# Usage: ngc-809/tools/start_apps.sh [stop]
set -uo pipefail
K='export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml'
hub() { ssh ngc-747-hub "$K; $1"; }
if [ "${1:-}" = stop ]; then
  hub "oc -n dpdk exec testpmd -- bash -c 'pkill -INT -x dpdk-testpmd; sleep 3; ip link set net1 promisc off; ip link show net1 | head -1'"
  exit 0
fi
hub "oc -n dpdk exec trex -- bash -c 'pgrep -x _t-rex-64 >/dev/null || (cd /opt/trex; nohup ./t-rex-64 -i --cfg /etc/trex_cfg.yaml > /tmp/trex_server.log 2>&1 < /dev/null & disown)'"
hub "oc -n dpdk exec testpmd -- bash -c 'pgrep -x dpdk-testpmd >/dev/null || (nohup /tmp/run_testpmd_bigring.sh > /tmp/testpmd_bigring.log 2>&1 & disown)'"
echo "waiting 50 s for TRex server and testpmd"; sleep 50
check() {  # <pod> <process name> <log>
  if hub "oc -n dpdk exec $1 -- pgrep -x $2" >/dev/null 2>&1; then echo "$1: $2 running"
  else echo "$1: $2 is NOT running. Last log lines ($3):"; hub "oc -n dpdk exec $1 -- tail -n 20 $3" 2>&1; ok=0; fi
}
ok=1
check trex _t-rex-64 /tmp/trex_server.log
check testpmd dpdk-testpmd /tmp/testpmd_bigring.log
[ $ok = 1 ] || { echo "not starting the sanity run" >&2; exit 1; }
hub "oc -n dpdk exec testpmd -- bash -c 'ip addr add 10.10.10.1/24 dev net1; echo x > /dev/udp/10.10.10.2/9; ip addr del 10.10.10.1/24 dev net1'; \
     oc -n dpdk exec trex -- bash -c 'ip addr add 10.10.10.2/24 dev net1; echo x > /dev/udp/10.10.10.1/9; ip addr del 10.10.10.2/24 dev net1'" >/dev/null 2>&1
echo "sanity: $(hub "oc -n dpdk exec trex -- bash -c 'cd /opt/trex; python3 /tmp/trex_e2e_run.py 64 10kpps 5'" 2>/dev/null | tail -1)"
