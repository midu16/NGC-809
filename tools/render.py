#!/usr/bin/env python3
"""Render an NGC-809 kustomize overlay and split it into the staged 01-07 manifests.

`kubectl kustomize <overlay>` outputs every object in one stream. tools/apply.sh has to apply
them in stages (NNCP -> SriovNetworkNodePolicy -> SriovNetwork -> ConfigMaps -> pods, with waits
in between; see README §4), so this writes one file per stage, using the same names as the root
manifests:
  01-namespace.yaml  02-nncp-vf-partition.yaml  03-sriovnetworknodepolicies.yaml
  04-sriovnetworks.yaml  05-configmaps-trex.yaml  06-pods-trex-testpmd.yaml  07-pods-iperf3.yaml

It exits non-zero if any "<placeholder>" is left in the rendered output.
With --check, it also compares every rendered object with the live-exported root 01-07 manifests
(parsed YAML, types included) and exits non-zero on any difference.

Usage (from ngc-809/):
  python3 tools/render.py kustomize/example-overlays-config/cwl                 # -> rendered/cwl/
  python3 tools/render.py kustomize/example-overlays-config/cwl --check
  python3 tools/render.py kustomize/example-overlays-config/<site> -o <dir>
  tools/apply.sh rendered/<site>                                                # staged apply
"""
import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGES = [  # (file, predicate on object)
    ("01-namespace.yaml", lambda o: o["kind"] == "Namespace"),
    ("02-nncp-vf-partition.yaml", lambda o: o["kind"] == "NodeNetworkConfigurationPolicy"),
    ("03-sriovnetworknodepolicies.yaml", lambda o: o["kind"] == "SriovNetworkNodePolicy"),
    ("04-sriovnetworks.yaml", lambda o: o["kind"] == "SriovNetwork"),
    ("05-configmaps-trex.yaml", lambda o: o["kind"] == "ConfigMap"),
    ("06-pods-trex-testpmd.yaml", lambda o: o["kind"] == "Pod" and o["metadata"]["name"] in ("trex", "testpmd")),
    ("07-pods-iperf3.yaml", lambda o: o["kind"] == "Pod"),
]
# "<namespace>", "<server-hostname>" ... ; does not match "pad_len < 0" in the TRex profile.
PLACEHOLDER = re.compile(r"<[a-z0-9][a-z0-9.-]*>")


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def key(o):
    m = o["metadata"]
    return (o["kind"], m.get("namespace"), m["name"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("overlay", help="kustomize overlay directory")
    ap.add_argument("-o", "--out", help="output dir (default: rendered/<overlay-basename>)")
    ap.add_argument("--check", action="store_true", help="compare with the root 01-07 live-export manifests")
    a = ap.parse_args()

    out = a.out or os.path.join(ROOT, "rendered", os.path.basename(os.path.normpath(a.overlay)))
    text = subprocess.run(["kubectl", "kustomize", a.overlay], check=True, capture_output=True, text=True).stdout
    objs = [o for o in yaml.safe_load_all(text) if o]

    left = sorted(set(PLACEHOLDER.findall(text)))
    if left:
        print(f"{ts()} ERROR unresolved placeholders in {a.overlay}: {' '.join(left)}", file=sys.stderr)
        return 1

    os.makedirs(out, exist_ok=True)
    rest = list(objs)
    for fname, pred in STAGES:
        sel = [o for o in rest if pred(o)]
        rest = [o for o in rest if not pred(o)]
        with open(os.path.join(out, fname), "w") as f:
            f.write(f"# NGC-809 — rendered from {os.path.relpath(a.overlay, ROOT)} by tools/render.py. Do not edit.\n")
            for o in sel:
                f.write("---\n" + yaml.safe_dump(o, sort_keys=False, width=120))
        print(f"{ts()} wrote {os.path.relpath(os.path.join(out, fname), ROOT)} ({len(sel)} objects)")
    if rest:
        print(f"{ts()} ERROR objects with no stage: {[key(o) for o in rest]}", file=sys.stderr)
        return 1

    if a.check:
        live = {}
        for p in sorted(glob.glob(os.path.join(ROOT, "0[1-7]-*.yaml"))):
            for o in yaml.safe_load_all(open(p)):
                if o:
                    live[key(o)] = o
        rend = {key(o): o for o in objs}
        bad = 0
        for k in sorted(set(live) | set(rend), key=str):
            if live.get(k) != rend.get(k):
                bad += 1
                print(f"{ts()} DIFF {k}")
                sys.stdout.writelines(difflib.unified_diff(
                    json.dumps(live.get(k), indent=1, sort_keys=True).splitlines(True),
                    json.dumps(rend.get(k), indent=1, sort_keys=True).splitlines(True), "live", "rendered", n=1))
        print(f"{ts()} check: {len(rend) - bad}/{len(set(live) | set(rend))} objects identical to the live export")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
