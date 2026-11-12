#!/usr/bin/env bash
# NGC-809: re-create the full 25 %/75 % VF split test bed on CWL from the manifests in ngc-809/, in the safe order.
# Runs from the laptop through the hub (ssh ngc-747-hub, KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml).
# Order and waits (each step waits for the cluster to settle before the next):
#   01 namespace -> 02 NNCPs (wait NNCE Available) -> 03 SriovNetworkNodePolicies (wait both SriovNetworkNodeStates
#   Succeeded AND the 4 resources allocatable) -> 04 SriovNetworks (wait the 4 NADs) -> 05 ConfigMaps ->
#   06 trex/testpmd + 07 iperf3 pods (wait Ready) -> restore in-pod files (tools/restore_pod_files.sh).
# Never deletes anything. If the trex/testpmd/iperf3 pods already exist, step 03 or a changed 02 will break their
# VF attachment (config-daemon rebind / VLAN reset) -> delete and re-create those pods afterwards.
# Usage: ngc-809/tools/apply.sh [MANIFEST_DIR]
#   MANIFEST_DIR: directory with the 01-07 files, default ngc-809/ (the live export). For a kustomize
#   overlay, render it first: python3 tools/render.py kustomize/example-overlays-config/cwl -> rendered/cwl.
#   NOTE: the wait checks below still use the CWL hostnames (M0/AW) and the fixed NGC-809 object names.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=${1:-.}
K='export KUBECONFIG=/root/kubeconfigs/cwl/kubeconfig.yaml'
S=openshift-sriov-network-operator
M0=master-0.cwl.integration.core.bos2.lab AW=appworker-1.cwl.integration.core.bos2.lab
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
hub() { ssh ngc-747-hub "$K; $1"; }
apply() { echo "$(ts) apply $1"; hub "oc apply -f -" < "$1" 2>&1 | grep -v '^Warning: would violate PodSecurity' || true; }
waitfor() {  # <description> <remote command that prints OK when done> [tries]
  local i
  for i in $(seq 1 "${3:-40}"); do
    if [ "$(hub "$2" 2>/dev/null)" = OK ]; then echo "$(ts) ok: $1"; return 0; fi
    sleep 8
  done
  echo "$(ts) TIMEOUT: $1" >&2; exit 1
}

apply "$DIR/01-namespace.yaml"
apply "$DIR/02-nncp-vf-partition.yaml"
sleep 10
waitfor "NNCPs Available" "for p in ngc809-master0-nic2-vf-partition ngc809-nic2-vf-partition; do oc get nncp \$p -o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}'; echo; done | grep -qv True || echo OK"
apply "$DIR/03-sriovnetworknodepolicies.yaml"
sleep 15
waitfor "SriovNetworkNodeStates Succeeded" "for n in $M0 $AW; do oc -n $S get sriovnetworknodestate \$n -o jsonpath='{.status.syncStatus}'; echo; done | grep -qv Succeeded || echo OK"
waitfor "VF resources allocatable" "a=\$(oc get node $M0 -o jsonpath='{.status.allocatable.openshift\.io/ngc809_srv_nic2_vf} {.status.allocatable.openshift\.io/ngc809_m0_vf0}'); b=\$(oc get node $AW -o jsonpath='{.status.allocatable.openshift\.io/ngc809_nic2_vf1} {.status.allocatable.openshift\.io/ngc809_aw1_vf0}'); [ \"\$a \$b\" = '1 1 1 1' ] && echo OK"
apply "$DIR/04-sriovnetworks.yaml"
waitfor "NADs present" "[ \$(oc -n dpdk get net-attach-def ngc809-master0-sink ngc809-tenant-75 ngc809-vf0-25-m0 ngc809-vf0-25-aw1 --no-headers 2>/dev/null | wc -l) = 4 ] && echo OK"
apply "$DIR/05-configmaps-trex.yaml"
apply "$DIR/06-pods-trex-testpmd.yaml"
apply "$DIR/07-pods-iperf3.yaml"
echo "$(ts) waiting for pods Ready"
hub "oc -n dpdk wait pod/trex pod/testpmd pod/iperf3-server pod/iperf3-client --for=condition=Ready --timeout=300s"
tools/restore_pod_files.sh
echo "$(ts) VF state on both hosts:"
for n in $AW $M0; do hub "oc debug -q node/$n -- chroot /host ip link show ens1f0np0 | grep 'vf '" | sed "s/^ */  ${n%%.*} /"; done
echo "$(ts) done. Start the DPDK apps with tools/start_apps.sh, then run the tests (README.md §5)."
