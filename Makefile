# KubeRay Batch Inference - single entry point for all operations.
#
# Targets are grouped by lifecycle stage. Run `make help` for the full list.

.DEFAULT_GOAL := help
SHELL := /bin/bash

# ─── Versions ────────────────────────────────────────────────────────
KIND_VERSION            ?= v0.27.0
K8S_VERSION             ?= v1.29.4
KUBERAY_VERSION         ?= 1.6.0
RAY_VERSION             ?= 2.54.1
PYTHON_VERSION          ?= 3.11

# ─── Names ───────────────────────────────────────────────────────────
CLUSTER_NAME            ?= kuberay-dev
NAMESPACE               ?= ray
KUBERAY_NAMESPACE       ?= kuberay-system
RAYCLUSTER_NAME         ?= qwen-raycluster
API_IMAGE               ?= local/batch-api:dev
WORKER_IMAGE            ?= local/ray-worker:$(RAY_VERSION)-cpu

# ─── Help ────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ─── Prerequisites ───────────────────────────────────────────────────
.PHONY: check-tools
check-tools: ## Verify required tools are installed
	@for t in docker kind kubectl helm jq curl; do \
		command -v $$t >/dev/null 2>&1 || { echo "ERROR: $$t not found. Run 'scripts/setup.sh'."; exit 1; }; \
	done
	@echo "✓ All required tools present"

# ─── Lifecycle: kind cluster ─────────────────────────────────────────
.PHONY: cluster-up
cluster-up: check-tools ## Create the local kind cluster
	@if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
		echo "✓ kind cluster '$(CLUSTER_NAME)' already exists"; \
	else \
		kind create cluster --config k8s/kind/kind-config.yaml --image kindest/node:$(K8S_VERSION); \
	fi
	kubectl cluster-info --context kind-$(CLUSTER_NAME)

.PHONY: cluster-down
cluster-down: ## Delete the local kind cluster
	kind delete cluster --name $(CLUSTER_NAME)

# ─── Lifecycle: KubeRay operator ─────────────────────────────────────
.PHONY: kuberay-install
kuberay-install: ## Install the KubeRay operator via Helm
	helm repo add kuberay https://ray-project.github.io/kuberay-helm/ 2>/dev/null || true
	helm repo update
	helm upgrade --install kuberay-operator kuberay/kuberay-operator \
		--version $(KUBERAY_VERSION) \
		--namespace $(KUBERAY_NAMESPACE) \
		--create-namespace \
		--wait
	kubectl -n $(KUBERAY_NAMESPACE) rollout status deploy/kuberay-operator --timeout=180s

.PHONY: kuberay-uninstall
kuberay-uninstall: ## Remove the KubeRay operator
	helm uninstall kuberay-operator -n $(KUBERAY_NAMESPACE) || true
	kubectl delete namespace $(KUBERAY_NAMESPACE) --ignore-not-found

# ─── Lifecycle: Images ───────────────────────────────────────────────
.PHONY: build-api
build-api: ## Build the FastAPI proxy image
	docker build -t $(API_IMAGE) -f api/Dockerfile api/

.PHONY: build-worker
build-worker: ## Build the Ray worker image with Qwen2.5-0.5B baked in
	docker build -t $(WORKER_IMAGE) -f inference/Dockerfile inference/

.PHONY: build-images
build-images: build-api build-worker ## Build all container images

.PHONY: load-images
load-images: ## Load custom images into the kind cluster
	kind load docker-image $(API_IMAGE) --name $(CLUSTER_NAME)
	kind load docker-image $(WORKER_IMAGE) --name $(CLUSTER_NAME)

# ─── Lifecycle: Namespace + storage + postgres ───────────────────────
.PHONY: namespace
namespace: ## Create the ray namespace
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -

.PHONY: storage
storage: namespace ## Apply shared PVC for batch inputs and outputs
	kubectl apply -n $(NAMESPACE) -f k8s/storage/shared-pvc.yaml
	@# Ensure the hostPath inside the kind node is world-writable. Kind creates
	@# extraMount targets with 0755 root:root by default, which causes the API
	@# pod's first POST /v1/batches to fail with Errno 13 on mkdir(/data/batches/...).
	@# Chmod-ing here (instead of relying on up.sh to chmod the host side) makes
	@# the fix independent of how make up was invoked and survives host/kind-node
	@# permission-bit drift.
	docker exec $(CLUSTER_NAME)-control-plane chmod -R 0777 /mnt/data

.PHONY: postgres
postgres: namespace ## Deploy Postgres for job metadata
	kubectl apply -n $(NAMESPACE) -f k8s/postgres/
	kubectl -n $(NAMESPACE) rollout status deploy/postgres --timeout=120s

# ─── Lifecycle: RayCluster + API ─────────────────────────────────────
.PHONY: raycluster
raycluster: namespace storage ## Apply the RayCluster manifest
	kubectl apply -n $(NAMESPACE) -f k8s/raycluster/raycluster.yaml
	@echo "Waiting for RayCluster to become ready..."
	@for i in $$(seq 1 60); do \
		state=$$(kubectl -n $(NAMESPACE) get raycluster $(RAYCLUSTER_NAME) -o jsonpath='{.status.state}' 2>/dev/null); \
		if [ "$$state" = "ready" ]; then echo "✓ RayCluster ready"; exit 0; fi; \
		sleep 5; \
	done; \
	echo "ERROR: RayCluster did not become ready in 5 minutes"; \
	kubectl -n $(NAMESPACE) get pods; \
	exit 1

.PHONY: api
api: namespace postgres ## Deploy the FastAPI proxy
	kubectl apply -n $(NAMESPACE) -f k8s/api/
	kubectl -n $(NAMESPACE) rollout status deploy/batch-api --timeout=120s

# ─── Lifecycle: Full up / down ───────────────────────────────────────
.PHONY: up
up: cluster-up kuberay-install build-images load-images namespace storage postgres raycluster api port-forward ## Bring up EVERYTHING: kind + KubeRay + RayCluster + API + postgres

.PHONY: down
down: ## Tear down everything (keeps the kind cluster - use `make cluster-down` to nuke)
	kubectl delete -n $(NAMESPACE) -f k8s/api/ --ignore-not-found
	kubectl delete -n $(NAMESPACE) -f k8s/raycluster/raycluster.yaml --ignore-not-found
	kubectl delete -n $(NAMESPACE) -f k8s/postgres/ --ignore-not-found
	kubectl delete -n $(NAMESPACE) -f k8s/storage/shared-pvc.yaml --ignore-not-found
	$(MAKE) kuberay-uninstall

# ─── Dev helpers ─────────────────────────────────────────────────────
.PHONY: port-forward
port-forward: ## Port-forward the FastAPI proxy to localhost:8000 (blocks)
	@echo "Forwarding http://localhost:8000 → batch-api"
	kubectl -n $(NAMESPACE) port-forward svc/batch-api 8000:8000

.PHONY: dashboard
dashboard: ## Port-forward the Ray dashboard to localhost:8265 (blocks)
	@echo "Ray dashboard: http://localhost:8265"
	kubectl -n $(NAMESPACE) port-forward svc/$(RAYCLUSTER_NAME)-head-svc 8265:8265

# ─── Monitoring: Prometheus + Grafana ────────────────────────────────
.PHONY: monitoring-up
monitoring-up: ## Install Prometheus + Grafana + Ray dashboards (idempotent)
	bash scripts/install-monitoring.sh

.PHONY: monitoring-down
monitoring-down: ## Uninstall the monitoring stack and delete the namespace
	-helm uninstall grafana -n monitoring
	-helm uninstall prometheus -n monitoring
	-kubectl delete namespace monitoring

.PHONY: grafana
grafana: ## Port-forward Grafana to localhost:3000 (blocks). admin/admin
	@echo "Grafana: http://localhost:3000  (login: admin / admin)"
	kubectl -n monitoring port-forward svc/grafana 3000:80

.PHONY: prometheus
prometheus: ## Port-forward Prometheus to localhost:9090 (blocks)
	@echo "Prometheus: http://localhost:9090"
	kubectl -n monitoring port-forward svc/prometheus-server 9090:80

.PHONY: logs-api
logs-api: ## Tail API pod logs
	kubectl -n $(NAMESPACE) logs -f deploy/batch-api

.PHONY: logs-ray-head
logs-ray-head: ## Tail Ray head pod logs
	kubectl -n $(NAMESPACE) logs -f -l ray.io/node-type=head -c ray-head

.PHONY: logs-ray-workers
logs-ray-workers: ## Tail Ray worker pod logs
	kubectl -n $(NAMESPACE) logs -f -l ray.io/node-type=worker -c ray-worker

.PHONY: shell-api
shell-api: ## Open a shell inside the API pod
	kubectl -n $(NAMESPACE) exec -it deploy/batch-api -- /bin/bash

# ─── Smoke test ──────────────────────────────────────────────────────
.PHONY: smoke-test
smoke-test: ## Run the exact curl from the exercise PDF against localhost:8000
	bash scripts/smoke-test.sh

# ─── Python dev (API only) ───────────────────────────────────────────
.PHONY: install
install: ## Install API Python deps locally (uses uv if present, else pip)
	cd api && (command -v uv && uv sync || pip install -e .[dev])

.PHONY: lint
lint: ## Lint with ruff
	cd api && (command -v uv && uv run ruff check src tests || ruff check src tests)

.PHONY: format
format: ## Format with ruff
	cd api && (command -v uv && uv run ruff format src tests || ruff format src tests)

.PHONY: typecheck
typecheck: ## Type-check with mypy
	cd api && (command -v uv && uv run mypy src || mypy src)

.PHONY: test
test: ## Run the pytest suite
	cd api && (command -v uv && uv run pytest || pytest)

.PHONY: ci
ci: lint typecheck test ## Run the full CI suite locally
