#!/usr/bin/env python3
"""Kernel-netdev traffic generator for the NGC-809 VF rate-cap test.

Runs INSIDE the trex pod. Sends 1500 B (incl. FCS) UDP frames
50:00:00:00:00:01 -> 60:00:00:00:00:01 (testpmd VF, learned on the switch, so no
flooding) out of the pod's kernel netdev `net1` (= appworker-1 ens1f0np0 VF1) through
an AF_PACKET socket with PACKET_QDISC_BYPASS, for <secs> seconds, and prints one JSON
line with frames sent and the achieved L2 rate.

Purpose: the same VF carries this kernel traffic through the mlx5e netdev send queues
instead of TRex's DPDK mlx5 PMD send queues, so comparing both against the same
max_tx_rate shows whether the E-Switch vport rate limiter acts on either datapath.

Usage: python3 kernel_vf_blaster.py <secs> [ifname]
"""
import json
import socket
import struct
import sys
import time

secs = float(sys.argv[1])
ifname = sys.argv[2] if len(sys.argv) > 2 else "net1"
PACKET_QDISC_BYPASS = 20
SOL_PACKET = 263              # not exported by older Python socket modules

dst, src = bytes.fromhex("600000000001"), bytes.fromhex("500000000001")
payload_len = 1500 - 4 - 14 - 20 - 8          # frame 1500 B incl. FCS
ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + 8 + payload_len, 0, 0, 64, 17, 0,
                 socket.inet_aton("16.0.0.3"), socket.inet_aton("10.0.0.1"))
udp = struct.pack("!HHHH", 1027, 15, 8 + payload_len, 0)
frame = dst + src + b"\x08\x00" + ip + udp + b"k" * payload_len

s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.setsockopt(SOL_PACKET, PACKET_QDISC_BYPASS, 1)
s.bind((ifname, 0))
sent = errs = 0
t0 = time.time()
end = t0 + secs
send = s.send
while True:
    for _ in range(256):
        try:
            send(frame)
            sent += 1
        except OSError:
            errs += 1
    if time.time() >= end:
        break
el = time.time() - t0
print(json.dumps({"ifname": ifname, "secs": round(el, 3), "frames": sent, "send_errors": errs,
                  "frame_B_on_wire_excl_fcs": len(frame),
                  "L2_gbps": round(sent * len(frame) * 8 / el / 1e9, 3),
                  "kpps": round(sent / el / 1e3, 1)}))
