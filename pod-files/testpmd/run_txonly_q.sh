#!/bin/bash
# txonly with a selectable number of TX queues/cores: run_txonly_q.sh <secs> <wait> <nq>
S=${1:-10}; W=${2:-75}; Q=${3:-1}
CORES=$(seq -s, 10 $((10+Q)))
( sleep 4; echo "set promisc all off"; echo "set allmulti all off"; sleep $W; echo "start"; sleep $S; echo "stop"; echo "show port stats 0"; echo "quit" ) | dpdk-testpmd -l $CORES --no-huge -m 512 --iova-mode=va -a $PCIDEVICE_OPENSHIFT_IO_NGC809_SRV_NIC2_VF --file-prefix gen -- -i --forward-mode=txonly --eth-peer=0,50:00:00:00:00:01 --txpkts=1496 --tx-ip=16.0.0.2,10.0.0.2 --nb-cores=$Q --txq=$Q --rxq=$Q --total-num-mbufs=32768
