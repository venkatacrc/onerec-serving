#!/usr/bin/env bash
# Bootstraps a single-node Kubernetes control plane (k3s) on this same
# 8x B200/GB200 box, plus everything the production-grade layer in
# platform/ needs on top of it: the NVIDIA device plugin (so
# nvidia.com/gpu becomes a schedulable resource), Helm, KEDA (autoscaling),
# kube-prometheus-stack (Prometheus/Grafana/Alertmanager), DCGM-exporter
# (GPU metrics), and an OpenTelemetry Collector + Jaeger (tracing).
#
# Idempotent: re-running skips steps that already succeeded.
#
# See docs/PRODUCTION_ARCHITECTURE.md §3 for why each of these was chosen,
# and §8 for what changes if you later point this at a real multi-node
# cluster instead of running this script at all.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$REPO_ROOT/scripts/common.sh"

OBSERVABILITY_NS="onerec-observability"
KEDA_NS="keda"

log_info "Step 1/6: k3s control plane"
if command -v k3s >/dev/null 2>&1 && systemctl is-active --quiet k3s 2>/dev/null; then
  log_ok "k3s already installed and running"
else
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644 --disable traefik" sh -
  log_ok "k3s installed"
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config" 2>/dev/null || true
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config" 2>/dev/null || true
export KUBECONFIG="$HOME/.kube/config"

retry 30 kubectl get nodes >/dev/null 2>&1 || die "k3s API server never became reachable"
log_ok "k3s API reachable"

log_info "Step 2/6: Helm"
if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
need_cmd helm
log_ok "Helm ready: $(helm version --short 2>/dev/null)"

log_info "Step 3/6: NVIDIA device plugin (exposes nvidia.com/gpu as a schedulable resource)"
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.2/deployments/static/nvidia-device-plugin.yml
kubectl -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout=120s \
  || log_warn "device plugin daemonset not ready yet -- check 'kubectl -n kube-system get pods -l name=nvidia-device-plugin-ds'"
log_ok "NVIDIA device plugin applied"

log_info "Step 4/6: KEDA (autoscaling on queue depth / GPU utilization, not just CPU)"
helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install keda kedacore/keda --namespace "$KEDA_NS" --create-namespace --wait --timeout 5m
log_ok "KEDA installed"

log_info "Step 5/6: kube-prometheus-stack (Prometheus + Grafana + Alertmanager) + DCGM-exporter"
kubectl create namespace "$OBSERVABILITY_NS" --dry-run=client -o yaml | kubectl apply -f -
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace "$OBSERVABILITY_NS" \
  --values "$SCRIPT_DIR/observability/kube-prometheus-values.yaml" \
  --wait --timeout 10m
kubectl apply -f "$SCRIPT_DIR/observability/dcgm-exporter.yaml"
kubectl apply -f "$SCRIPT_DIR/observability/prometheus-rules.yaml"
log_ok "Observability stack installed in namespace '$OBSERVABILITY_NS'"

log_info "Step 6/6: OpenTelemetry Collector + Jaeger (distributed tracing)"
kubectl apply -f "$SCRIPT_DIR/observability/otel-collector.yaml"
kubectl apply -f "$SCRIPT_DIR/observability/jaeger.yaml"
log_ok "Tracing stack installed"

cat <<EOF

$(log_ok "Bootstrap complete.")

Next steps:
  1. Build + load the router and feature-store images (or push to a registry k3s can pull from):
       docker build -t onerec-router:latest platform/router/
       docker build -t onerec-feature-store:latest platform/feature_store/
       # k3s uses containerd directly; for local-only images either push to a
       # registry or: docker save onerec-router:latest | sudo k3s ctr images import -

  2. Apply the platform manifests:
       kubectl apply -f platform/k8s/00-namespace.yaml
       kubectl apply -f platform/k8s/

  3. Check rollout:
       kubectl -n onerec get pods -w

  4. Grafana (default admin / kube-prometheus-stack values):
       kubectl -n $OBSERVABILITY_NS port-forward svc/kube-prometheus-stack-grafana 3000:80
       # http://localhost:3000 -- import platform/observability/grafana-dashboard-onerec.json

  5. Jaeger UI:
       kubectl -n $OBSERVABILITY_NS port-forward svc/jaeger-query 16686:16686
       # http://localhost:16686

  6. Router:
       kubectl -n onerec port-forward svc/onerec-router 9000:9000
       curl localhost:9000/status
EOF
