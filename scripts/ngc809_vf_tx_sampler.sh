#!/usr/bin/env bash
# Runs ON A NODE (oc debug ... chroot /host bash -s <pf> <vf> <samples>): prints one line per second:
#   epoch vf_tx_bytes vf_tx_pkts vf_rx_bytes vf_rx_pkts phy_tx_bytes phy_rx_bytes tx_pause rx_pause
# VF counters are the hardware per-VF counters as seen by the PF (ip -s link, include DPDK traffic);
# PHY counters are the wire (ethtool -S *_phy). Lets per-second rates be computed at every NIC hop
# independent of the generator's own timing (TRex stretches runs on a capped VF).
PF=${1:-ens1f0np0} VF=${2:-1} N=${3:-30}
for i in $(seq 1 "$N"); do
  t=$(date +%s.%N)
  v=$(ip -s link show "$PF" | awk -v vf="$VF" '
        $1=="vf" && $2==vf {f=1; next}
        f && /RX: bytes/ {getline; rb=$1; rp=$2; next}
        f && /TX: bytes/ {getline; print $1, $2, rb, rp; exit}')
  e=$(ethtool -S "$PF" | awk '/ tx_bytes_phy:/{a=$2} / rx_bytes_phy:/{d=$2} / tx_pause_ctrl_phy:/{b=$2} / rx_pause_ctrl_phy:/{c=$2} END{print a, d, b, c}')
  echo "$t $v $e"
  sleep 1
done
