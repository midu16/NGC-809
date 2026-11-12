#!/usr/bin/env bash
# NGC-809: copy the in-pod helper files (pod-files/) into the trex and testpmd pods after they are (re)created.
#   trex:    /etc/trex_cfg.yaml + /tmp/trex_e2e_run.py, trex_e2e_capture.py, trex_l2_test.py, trex_run.py, kernel_vf_blaster.py
#   testpmd: /tmp/run_testpmd.sh, run_testpmd_bigring.sh, run_txonly.sh, run_txonly_q.sh
# /etc/trex_cfg.yaml hard-codes the VF PCI address and the CPUs (master 3, latency 4, DP 67/68) that the CPU
# manager gave the trex pod on 2026-11-11. The script compares them with the new pod and warns on mismatch
# (then edit pod-files/trex/etc/trex_cfg.yaml or render /opt/templates/trex_cfg.yaml from the ConfigMap).
# Usage: ngc-809/tools/restore_pod_files.sh
set -euo pipefail
cd "$(dirname "$0")/.."
K='export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml'
hub() { ssh ngc-747-hub "$K; $1"; }
tar czf - -C pod-files/trex etc tmp | hub "oc -n dpdk exec -i trex -- tar xzf - -C /"
tar czf - -C pod-files/testpmd . | hub "oc -n dpdk exec -i testpmd -- bash -c 'tar xzf - -C /tmp && chmod +x /tmp/run_*.sh'"
echo "restored pod files into trex and testpmd"
cpus=$(hub "oc -n dpdk exec trex -- sh -c 'grep Cpus_allowed_list /proc/1/status | cut -f2; echo \$PCIDEVICE_OPENSHIFT_IO_NGC809_NIC2_VF1'" | tr '\n' ' ')
echo "trex pod: cpus/pci = $cpus (trex_cfg.yaml expects: 3-4,67-68 0000:12:00.3)"
[ "$cpus" = "3-4,67-68 0000:12:00.3 " ] || echo "WARNING: trex pod CPUs or VF differ from pod-files/trex/etc/trex_cfg.yaml — adjust it before starting t-rex-64" >&2
