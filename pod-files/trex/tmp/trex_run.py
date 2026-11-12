import sys, time
sys.path.insert(0, "/opt/trex/automation/trex_control_plane/interactive")
from trex.stl.api import *
size, rate, dur = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
c = STLClient(server="127.0.0.1"); c.connect(); c.acquire(ports=[0], force=True); c.reset(ports=[0])
c.set_service_mode(ports=[0], enabled=True); c.set_l2_mode(port=0, dst_mac="60:00:00:00:00:01"); c.set_service_mode(ports=[0], enabled=False)
base = Ether(src="50:00:00:00:00:01", dst="60:00:00:00:00:01")/IP(src="16.0.0.1", dst="10.0.0.1")/UDP(sport=1026, dport=15)
pkt = base/("x"*max(0, size-4-len(base)))
c.add_streams(STLStream(packet=STLPktBuilder(pkt=pkt), mode=STLTXCont()), ports=[0])
c.clear_stats(); c.start(ports=[0], mult=rate, duration=dur); c.wait_on_traffic(ports=[0]); time.sleep(2)
s = c.get_stats()[0]
loss = s["opackets"] - s["ipackets"]
print("size=%dB offered=%s dur=%ss | tx=%d rx=%d lost=%d (%.4f%%) | tx_bps=%.2fG rx_bps=%.2fG (L1 incl. 20B/frame)" % (
    size, rate, dur, s["opackets"], s["ipackets"], loss, 100.0*loss/max(1, s["opackets"]),
    s["obytes"]*8/dur/1e9 + s["opackets"]*20*8/dur/1e9, s["ibytes"]*8/dur/1e9 + s["ipackets"]*20*8/dur/1e9))
c.release(ports=[0]); c.disconnect()
