#!/usr/bin/env python3
"""Run one TRex STL L2 stream (port 0 -> testpmd MAC) and print a one-line JSON result.

Runs inside the trex pod against the local TRex server (127.0.0.1). Sets port 0 to L2
mode with destination 60:00:00:00:00:01 (testpmd VF), sends a single UDP stream of
<size> bytes (incl. 4-byte FCS) at <rate> (TRex multiplier, e.g. 10kpps, 18.75gbps)
for <duration> seconds, and reports TX/RX packets/bytes, loss and L1/L2 rates.
Optional 4th arg: pcap path -> captures the first 100 TX and RX packets of the run.

Usage: python3 trex_e2e_run.py <size> <rate> <duration> [capture.pcap]
The *_gbps fields divide by the nominal <duration>. TRex never drops frames the NIC does
not accept: on a rate-capped VF (max_tx_rate) the run simply takes longer, so those
fields overstate the rate. "elapsed_s" and the *_wall fields use the measured time from
start() to the end of traffic and are the ones to compare with a VF cap.
Env TREX_FORCE=1 passes force=True to start(), allowing an offered rate above line rate
(saturates the TX ring, used for the NGC-809 VF rate-cap test).
"""
import json
import os
import sys
import time

sys.path.insert(0, "/opt/trex/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient, STLStream, STLPktBuilder, STLTXCont, Ether, IP, UDP  # noqa: E402

size, rate, dur = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
cap = sys.argv[4] if len(sys.argv) > 4 else None

c = STLClient(server="127.0.0.1")
c.connect()
c.acquire(ports=[0], force=True)
c.reset(ports=[0])
c.set_service_mode(ports=[0], enabled=True)
c.set_l2_mode(port=0, dst_mac="60:00:00:00:00:01")
c.set_service_mode(ports=[0], enabled=False)
base = Ether(src="50:00:00:00:00:01", dst="60:00:00:00:00:01") / IP(src="16.0.0.1", dst="10.0.0.1") / UDP(sport=1026, dport=15)
pkt = base / ("x" * max(0, size - 4 - len(base)))
c.add_streams(STLStream(packet=STLPktBuilder(pkt=pkt), mode=STLTXCont()), ports=[0])
c.clear_stats()
cid = c.start_capture(tx_ports=[0], rx_ports=[0], limit=200)["id"] if cap else None
t0 = time.time()
c.start(ports=[0], mult=rate, duration=dur, force=os.environ.get("TREX_FORCE") == "1")
c.wait_on_traffic(ports=[0])
elapsed = time.time() - t0
time.sleep(2)
if cap:
    c.stop_capture(cid, output=cap)
s = c.get_stats()[0]
res = {
    "size_B": size, "offered": rate, "duration_s": dur,
    "tx_pkts": s["opackets"], "rx_pkts": s["ipackets"],
    "lost_pkts": s["opackets"] - s["ipackets"],
    "loss_pct": round(100.0 * (s["opackets"] - s["ipackets"]) / max(1, s["opackets"]), 6),
    "tx_L2_gbps": round(s["obytes"] * 8 / dur / 1e9, 3),
    "rx_L2_gbps": round(s["ibytes"] * 8 / dur / 1e9, 3),
    "tx_L1_gbps": round((s["obytes"] + 20 * s["opackets"]) * 8 / dur / 1e9, 3),
    "rx_L1_gbps": round((s["ibytes"] + 20 * s["ipackets"]) * 8 / dur / 1e9, 3),
    "tx_mpps": round(s["opackets"] / dur / 1e6, 3),
    "elapsed_s": round(elapsed, 2),
    "tx_L2_gbps_wall": round(s["obytes"] * 8 / elapsed / 1e9, 3),
    "tx_L1_gbps_wall": round((s["obytes"] + 20 * s["opackets"]) * 8 / elapsed / 1e9, 3),
    "oerrors": s.get("oerrors", 0), "ierrors": s.get("ierrors", 0),
    "capture": cap,
}
print(json.dumps(res))
c.release(ports=[0])
c.disconnect()
