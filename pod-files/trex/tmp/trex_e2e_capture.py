#!/usr/bin/env python3
"""Capture TRex port 0 TX+RX frames of a short low-rate L2 stream to testpmd (service mode).

In normal mode the mlx5 HW filters keep RX traffic off the CPU, so TRex captures are
taken in service mode at a low rate. Sends 64B UDP frames 50:00:00:00:00:01 ->
60:00:00:00:00:01 at <pps> for <secs> seconds and writes the capture to <pcap>.
Usage: python3 trex_e2e_capture.py <pps> <secs> <pcap>
"""
import sys, time
sys.path.insert(0, "/opt/trex/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient, STLStream, STLPktBuilder, STLTXCont, Ether, IP, UDP
pps, secs, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
c = STLClient(server="127.0.0.1"); c.connect(); c.acquire(ports=[0], force=True); c.reset(ports=[0])
c.set_service_mode(ports=[0], enabled=True)
c.set_l2_mode(port=0, dst_mac="60:00:00:00:00:01")
pkt = Ether(src="50:00:00:00:00:01", dst="60:00:00:00:00:01")/IP(src="16.0.0.1", dst="10.0.0.1")/UDP(sport=1026, dport=15)/("x"*18)
c.add_streams(STLStream(packet=STLPktBuilder(pkt=pkt), mode=STLTXCont(pps=pps)), ports=[0])
cid = c.start_capture(tx_ports=[0], rx_ports=[0], limit=1000)["id"]
c.clear_stats(); c.start(ports=[0], duration=secs, force=True); c.wait_on_traffic(ports=[0]); time.sleep(1)
c.stop_capture(cid, output=out)
s = c.get_stats()[0]; print("tx=%d rx=%d" % (s["opackets"], s["ipackets"]))
c.set_service_mode(ports=[0], enabled=False); c.release(ports=[0]); c.disconnect()
