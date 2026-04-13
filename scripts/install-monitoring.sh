#!/usr/bin/env bash
# -------------------------------------------------------------------------
# install-monitoring.sh - Bring up Prometheus + Grafana + Ray dashboard
# auto-provisioning for the existing RayCluster.
#
# Prerequisites (must already be running - script does NOT set these up):
#   - kind cluster with the RayCluster healthy (`make up` has completed)
#   - kubectl current-context points at the kind cluster
#   - helm installed
#   - curl and jq installed
#
# Steps:
#   1. Add helm repos if missing
#   2. Install Prometheus (prometheus-community chart) into `monitoring` ns
#   3. Install Grafana (grafana chart) into `monitoring` ns
#   4. Extract Ray's default dashboard JSONs from the head pod and create
#      a ConfigMap (labeled for Grafana sidecar auto-import)
#   5. Restart the Ray head pod so it picks up the RAY_PROMETHEUS_HOST,
#      RAY_GRAFANA_HOST, RAY_GRAFANA_IFRAME_HOST env vars (which are
#      declared in k8s/raycluster/raycluster.yaml - re-apply it first
#      if those env vars aren't set on the running pod)
#
# Idempotent: re-running upgrades the helm releases, refreshes the
# ConfigMap, restarts the head pod only if its env vars are missing.
# -------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MONITORING_NS="monitoring"
RAY_NS="ray"
RAYCLUSTER_NAME="qwen-raycluster"

log()  { printf "\033[0;32m[monitoring]\033[0m %s\n" "$*"; }
warn() { printf "\033[0;33m[monitoring]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[0;31m[monitoring]\033[0m %s\n" "$*" >&2; exit 1; }

# ─── 0. Pre-flight checks ───────────────────────────────────────────────
for t in kubectl helm curl; do
  command -v "$t" >/dev/null 2>&1 || die "$t not found - run scripts/setup.sh first"
done

log "Verifying RayCluster is healthy..."
state=$(kubectl -n "$RAY_NS" get raycluster "$RAYCLUSTER_NAME" -o jsonpath='{.status.state}' 2>/dev/null || echo "")
if [[ "$state" != "ready" ]]; then
  die "RayCluster '$RAYCLUSTER_NAME' is not ready (state='$state'). Run 'make up' first."
fi

HEAD_POD=$(kubectl -n "$RAY_NS" get pod \
  -l "ray.io/cluster=$RAYCLUSTER_NAME,ray.io/node-type=head" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
[[ -n "$HEAD_POD" ]] || die "Could not find the Ray head pod"

# ─── 1. Re-apply RayCluster manifest so env vars are up to date ─────────
log "Re-applying RayCluster manifest (ensures env vars are current)..."
kubectl apply -n "$RAY_NS" -f k8s/raycluster/raycluster.yaml >/dev/null

# ─── 2. Helm repos ──────────────────────────────────────────────────────
log "Adding helm repos (prometheus-community, grafana)..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

# ─── 3. Namespace ───────────────────────────────────────────────────────
kubectl create namespace "$MONITORING_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# ─── 4. Prometheus ──────────────────────────────────────────────────────
log "Installing Prometheus via helm (release=prometheus, ns=$MONITORING_NS)..."
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace "$MONITORING_NS" \
  --values k8s/monitoring/prometheus-values.yaml \
  --wait --timeout 5m

# ─── 5. Grafana ─────────────────────────────────────────────────────────
log "Installing Grafana via helm (release=grafana, ns=$MONITORING_NS)..."
helm upgrade --install grafana grafana/grafana \
  --namespace "$MONITORING_NS" \
  --values k8s/monitoring/grafana-values.yaml \
  --wait --timeout 5m

# ─── 6. Extract Ray dashboards from the head pod and create ConfigMap ──
# Ray ships a set of default Grafana dashboards inside the head pod at
# /tmp/ray/session_latest/metrics/grafana/dashboards/. We pull them out
# and turn each one into a key in a ConfigMap labeled so the Grafana
# sidecar auto-imports them.
log "Extracting Ray default dashboards from head pod: $HEAD_POD"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

DASHBOARD_DIR="/tmp/ray/session_latest/metrics/grafana/dashboards"
if ! kubectl -n "$RAY_NS" exec "$HEAD_POD" -- test -d "$DASHBOARD_DIR" 2>/dev/null; then
  warn "Ray default dashboards not found at $DASHBOARD_DIR inside the head pod."
  warn "Skipping dashboard import. You can add dashboards manually via Grafana UI."
else
  # List dashboard files
  DASHBOARDS=$(kubectl -n "$RAY_NS" exec "$HEAD_POD" -- \
    sh -c "ls $DASHBOARD_DIR/*.json 2>/dev/null" || echo "")

  if [[ -z "$DASHBOARDS" ]]; then
    warn "No dashboard JSON files found in $DASHBOARD_DIR. Skipping import."
  else
    log "Copying dashboards to local tmp..."
    for remote_path in $DASHBOARDS; do
      fname=$(basename "$remote_path")
      kubectl -n "$RAY_NS" exec "$HEAD_POD" -- cat "$remote_path" > "$TMPDIR/$fname"
      log "  + $fname ($(wc -c < "$TMPDIR/$fname") bytes)"
    done

    # Build a --from-file list and create the ConfigMap
    FROM_FILE_ARGS=()
    for f in "$TMPDIR"/*.json; do
      [[ -f "$f" ]] || continue
      FROM_FILE_ARGS+=(--from-file="$f")
    done

    if [[ ${#FROM_FILE_ARGS[@]} -gt 0 ]]; then
      log "Creating/updating grafana-ray-dashboards ConfigMap..."
      kubectl -n "$MONITORING_NS" create configmap grafana-ray-dashboards \
        "${FROM_FILE_ARGS[@]}" \
        --dry-run=client -o yaml \
        | kubectl label --local -f - -o yaml grafana_dashboard=1 \
        | kubectl apply -f -
    fi
  fi
fi

# ─── 7. Restart Ray head so env vars (RAY_PROMETHEUS_HOST etc) apply ───
log "Restarting Ray head pod so RAY_PROMETHEUS_HOST/RAY_GRAFANA_HOST env vars take effect..."
kubectl -n "$RAY_NS" delete pod "$HEAD_POD" --wait=false >/dev/null
log "Waiting up to 3 minutes for the new head pod to become Ready..."
for i in $(seq 1 36); do
  new_head=$(kubectl -n "$RAY_NS" get pod \
    -l "ray.io/cluster=$RAYCLUSTER_NAME,ray.io/node-type=head" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -n "$new_head" && "$new_head" != "$HEAD_POD" ]]; then
    ready=$(kubectl -n "$RAY_NS" get pod "$new_head" \
      -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo "false")
    if [[ "$ready" == "true" ]]; then
      log "New head pod $new_head is Ready"
      break
    fi
  fi
  sleep 5
done

# ─── Done ───────────────────────────────────────────────────────────────
cat <<EOF

[monitoring] Monitoring stack installed. Next steps:

  1. Port-forward Grafana to localhost:3000 (in a dedicated terminal):
       make grafana

  2. Port-forward Prometheus to localhost:9090 (optional):
       kubectl -n monitoring port-forward svc/prometheus-server 9090:80

  3. Grafana login: admin / admin (demo only)

  4. Refresh the Ray dashboard at http://localhost:8265/#/metrics
     The time-series charts should now render (iframed from Grafana).

  5. Tear down:
       make monitoring-down

EOF
