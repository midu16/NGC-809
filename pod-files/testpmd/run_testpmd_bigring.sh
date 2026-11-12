#!/bin/bash
# dpdk-testpmd mac-forward sink with promiscuous mode OFF (trusted VF + promisc reflects uplink traffic -> STP loopback block)
( sleep 4; echo "set promisc all off"; echo "set allmulti all off"; echo "start"; sleep 1700; echo "show port stats 0"; echo "stop"; echo "quit" ) | dpdk-testpmd -l 10,11 --no-huge -m 512 --iova-mode=va -a $PCIDEVICE_OPENSHIFT_IO_NGC809_SRV_NIC2_VF --file-prefix sink -- -i --forward-mode=mac --eth-peer=0,50:00:00:00:00:01 --total-num-mbufs=32768 --txd=4096 --rxd=4096
