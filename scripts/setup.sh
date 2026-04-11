#!/usr/bin/env bash
# -------------------------------------------------------------------------
# setup.sh — Install all prerequisites for the KubeRay Batch Inference
# demo on a fresh Ubuntu 22.04 machine.
#
# Installs: docker, kind, kubectl, helm, jq, curl, make, python3.11, uv
#
# Idempotent: re-running skips tools already present at the required version.
# Must be run with sudo access. Uses apt where possible, curl-based installers
# where not.
# -------------------------------------------------------------------------
set -euo pipefail

# ─── Versions ───────────────────────────────────────────────────────────
# Pins verified 2026-04: kind maintains wide backward compat on node
# images, so a newer kind binary still boots the v1.29.4 node image
# pinned in k8s/kind/kind-config.yaml.
KIND_VERSION="${KIND_VERSION:-v0.27.0}"
KUBECTL_VERSION="${KUBECTL_VERSION:-v1.29.4}"
HELM_VERSION="${HELM_VERSION:-v3.15.3}"

# ─── Helpers ────────────────────────────────────────────────────────────
log()  { printf "\033[0;32m[setup]\033[0m %s\n" "$*"; }
warn() { printf "\033[0;33m[setup]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[0;31m[setup]\033[0m %s\n" "$*" >&2; exit 1; }

need_root() {
  if [[ $EUID -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    die "This script needs sudo. Re-run with sudo or from a sudoer shell."
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) die "Unsupported architecture: $(uname -m)" ;;
  esac
}

# ─── 1. System packages ─────────────────────────────────────────────────
install_apt_packages() {
  log "Updating apt and installing base packages..."
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release \
    jq make build-essential git \
    python3 python3-venv python3-pip
}

# ─── 2. Docker ──────────────────────────────────────────────────────────
install_docker() {
  if have docker; then
    log "Docker already installed: $(docker --version)"
    return
  fi
  log "Installing Docker Engine..."
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(arch) signed-by=/etc/apt/keyrings/docker.gpg] \
     https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                          docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
  warn "Added $USER to the docker group. Log out and back in for it to take effect."
}

# ─── 3. kind ────────────────────────────────────────────────────────────
install_kind() {
  if have kind && [[ "$(kind version | awk '{print $2}')" == "$KIND_VERSION" ]]; then
    log "kind $KIND_VERSION already installed"
    return
  fi
  log "Installing kind $KIND_VERSION..."
  curl -fsSL -o /tmp/kind \
    "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-$(arch)"
  chmod +x /tmp/kind
  sudo mv /tmp/kind /usr/local/bin/kind
}

# ─── 4. kubectl ─────────────────────────────────────────────────────────
install_kubectl() {
  if have kubectl; then
    log "kubectl already installed: $(kubectl version --client=true | head -1)"
    return
  fi
  log "Installing kubectl $KUBECTL_VERSION..."
  curl -fsSL -o /tmp/kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(arch)/kubectl"
  chmod +x /tmp/kubectl
  sudo mv /tmp/kubectl /usr/local/bin/kubectl
}

# ─── 5. Helm ────────────────────────────────────────────────────────────
install_helm() {
  if have helm; then
    log "helm already installed: $(helm version --short)"
    return
  fi
  log "Installing helm $HELM_VERSION..."
  curl -fsSL -o /tmp/helm.tgz \
    "https://get.helm.sh/helm-${HELM_VERSION}-linux-$(arch).tar.gz"
  tar -xzf /tmp/helm.tgz -C /tmp
  sudo mv "/tmp/linux-$(arch)/helm" /usr/local/bin/helm
  rm -rf /tmp/helm.tgz "/tmp/linux-$(arch)"
}

# ─── 6. uv (fast Python package manager) ────────────────────────────────
install_uv() {
  if have uv; then
    log "uv already installed: $(uv --version)"
    return
  fi
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Add uv to PATH for the current shell
  export PATH="$HOME/.local/bin:$PATH"
}

# ─── 7. Host data directory ─────────────────────────────────────────────
create_data_dir() {
  local path="/tmp/kuberay-batch-inference/data"
  log "Ensuring shared host data directory exists at $path"
  mkdir -p "$path"
  chmod 0777 "$path"
}

# ─── Main ───────────────────────────────────────────────────────────────
main() {
  need_root
  install_apt_packages
  install_docker
  install_kind
  install_kubectl
  install_helm
  install_uv
  create_data_dir

  log "All prerequisites installed."
  log "Tool versions:"
  docker  --version || true
  kind    version   || true
  kubectl version --client=true || true
  helm    version --short || true
  uv      --version || true

  warn "If you just installed Docker, start a new shell so the 'docker' group takes effect."
  log  "Next: run 'make up' to bring up the full stack."
}

main "$@"
