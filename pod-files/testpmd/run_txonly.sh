#!/bin/bash
# dpdk-testpmd txonly generator toward TRex VF MAC, promisc OFF, 1500B frames (1496 + FCS), 2 TX cores, runs <secs> seconds
S=${1:-10}; W=${2:-75}
( sleep 4; echo "set promisc all off"; echo "set allmulti all off"; sleep $W; echo "start"; sleep $S; echo "stop"; echo "show port stats 0"; echo "quit" ) | dpdk-testpmd -l 10,11,12 --no-huge -m 512 --iova-mode=va -a $PCIDEVICE_OPENSHIFT_IO_NGC809_SRV_NIC2_VF --file-prefix gen -- -i --forward-mode=txonly --eth-peer=0,50:00:00:00:00:01 --txpkts=1496 --tx-ip=16.0.0.2,10.0.0.2 --nb-cores=2 --txq=2 --rxq=2 --total-num-mbufs=32768
