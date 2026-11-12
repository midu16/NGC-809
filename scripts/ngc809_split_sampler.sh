#!/usr/bin/env bash
# Runs ON A NODE (oc debug node/<n> -- chroot /host bash -s <pf> <samples>): one line per second:
#   epoch vf0_tx_B vf0_rx_B vf1_tx_B vf1_rx_B phy_tx_B phy_rx_B
# VF counters = per-VF hardware counters seen by the PF (ip -s link; include kernel and DPDK traffic).
# PHY counters = wire (ethtool -S *_phy). Used to show the NGC-809 VF0 25 % / VF1 75 % split per second.
PF=${1:-ens1f0np0} N=${2:-30}
for i in $(seq 1 "$N"); do
  t=$(date +%s.%N)
  v=$(ip -s link show "$PF" | awk '
        /^ +vf [0-9]+ /{vf=$2; next}
        vf!="" && /RX: bytes/{getline; rx[vf]=$1; next}
        vf!="" && /TX: bytes/{getline; tx[vf]=$1; next}
        END{print tx[0]+0, rx[0]+0, tx[1]+0, rx[1]+0}')
  e=$(ethtool -S "$PF" | awk '/ tx_bytes_phy:/{a=$2} / rx_bytes_phy:/{b=$2} END{print a, b}')
  echo "$t $v $e"
  sleep 1
done
