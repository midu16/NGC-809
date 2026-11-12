from trex_stl_lib.api import *
from testpmd_addr import *

# Wild local MAC — must match trex-pod Multus annotation (article mac_localport0)
mac_localport0 = '<trex-mac>'
# Article also defines mac_localport1 / telco1 for dual-NIC loopback;
# this deployment uses a single VF on port 0 only (no port 1).

class STLS1(object):

    def __init__(self):
        # 1500 B L2 (FCS added by HW). 64 B frames saturate CPU (~8–9 Gbit/s)
        # before the 75% cap. 2026-11-06 line-rate proof used 1500 B.
        self.fsize = 1500
        self.number = 0

    def create_stream(self, direction=0):
        self.number = self.number + 1
        # direction ignored — only port 0 exists on this TRex pod
        # 50:…:01 → 60:…:01, UDP 1026 → 15 (same tuple as the 2026-11-06 proof)
        base_pkt = Ether(dst=mac_telco0, src=mac_localport0) / IP(src="16.0.0.1", dst=ip_telco0) / UDP(dport=15, sport=1026)
        pad_len = self.fsize - 4 - len(base_pkt)  # HW adds 4-byte FCS
        if pad_len < 0:
            pad_len = 0
        return STLStream(
            packet=STLPktBuilder(pkt=base_pkt / ('x' * pad_len)),
            mode=STLTXCont())

    def get_streams(self, direction=0, **kwargs):
        # Article uses range(14) for -c 14. Pod Guaranteed cpu:4 →
        # MASTER + LATENCY + ~2 TX threads → keep 2 streams (match -c 2).
        s = []
        for i in range(2):
            s.append(self.create_stream(direction=0))
        return s

def register():
    return STLS1()
