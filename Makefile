# AI Inference Battlefield
CLUSTER   := battlefield
NS        := battlefield
GW        := http://127.0.0.1:8080
ATK       := http://127.0.0.1:8081
CTL       := http://127.0.0.1:8082
PY_IMAGES := worker attacker edge
GO_IMAGES := gateway controller
GPU       ?= gpu-2
DUR       ?= 90

.PHONY: help cluster-up cluster-down build load deploy up restart status \
        defenses-naive defenses-hardened baseline spike ramp long-prompts noisy-tenant stop \
        chaos-slow chaos-kv chaos-errors chaos-reset kill-pod pod-killer \
        canary-start canary-status canary-abort eval console agent mission

help: ## list targets
	@grep -E '^[a-zA-Z_ -]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-20s %s\n", $$1, $$2}'

# ------------------------------------------------------------- cluster
cluster-up: ## create the kind cluster
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config deploy/kind/cluster.yaml --wait 240s

cluster-down: ## delete the kind cluster
	kind delete cluster --name $(CLUSTER)

build: ## build all images
	cd gateway && CGO_ENABLED=0 go build -o bin/gateway ./cmd/gateway
	cd controller && CGO_ENABLED=0 go build -o bin/controller ./cmd/controller
	for i in $(GO_IMAGES) $(PY_IMAGES); do docker build -q -t battlefield/$$i:dev $$i >/dev/null && echo "built $$i"; done

load: ## load images into kind
	for i in $(GO_IMAGES) $(PY_IMAGES); do kind load docker-image --name $(CLUSTER) battlefield/$$i:dev; done
	for i in prom/prometheus:v3.5.0 grafana/grafana:12.1.0; do \
	  docker exec $(CLUSTER)-control-plane crictl images -q $$i | grep -q . || kind load docker-image --name $(CLUSTER) $$i; done

deploy: ## apply manifests
	kubectl apply -f deploy/k8s/00-namespace.yaml
	kubectl apply -f deploy/k8s/

restart: ## roll battlefield deployments to pick up new images
	kubectl -n $(NS) rollout restart statefulset/gpu deploy/gpu-canary deploy/gateway deploy/attacker deploy/controller deploy/edge

up: cluster-up build load deploy ## everything: cluster, images, manifests
	kubectl -n $(NS) rollout status deploy/gateway --timeout=180s

status: ## pods + gateway view of the fleet
	kubectl -n $(NS) get pods -o wide
	@python3 scripts/bf.py status

# ------------------------------------------------------------- defenses
defenses-naive: ## turn every defense off
	curl -s -XPOST $(GW)/admin/defenses/preset/naive; echo
defenses-hardened: ## turn every defense on
	curl -s -XPOST $(GW)/admin/defenses/preset/hardened; echo

# ------------------------------------------------------------- traffic attacks
baseline spike ramp noisy-tenant: ## start a traffic scenario (DUR=seconds)
	@python3 scripts/bf.py attack $@ $(DUR)
long-prompts: ## long-prompt KV flood
	@python3 scripts/bf.py attack long_prompts $(DUR)
stop: ## stop all traffic
	curl -s -XPOST $(ATK)/attacks/stop; echo
watch: ## live client-side view of running attacks
	@python3 scripts/bf.py watch
events: ## recent gateway events
	@python3 scripts/bf.py events

# ------------------------------------------------------------- chaos
chaos-slow: ## make one GPU slow (GPU=gpu-2)
	curl -s -XPOST $(GW)/admin/chaos/$(GPU) -d '{"slow_ttft_ms":4000}'; echo
chaos-kv: ## squeeze one GPU's KV cache (GPU=gpu-2)
	curl -s -XPOST $(GW)/admin/chaos/$(GPU) -d '{"kv_budget":6000}'; echo
chaos-errors: ## make one GPU return 50% errors
	curl -s -XPOST $(GW)/admin/chaos/$(GPU) -d '{"error_rate":0.5}'; echo
chaos-reset: ## clear all injected faults
	curl -s -XPOST $(GW)/admin/chaos/all/reset; echo
kill-pod: ## delete a GPU pod (GPU=gpu-1)
	kubectl -n $(NS) delete pod $(GPU) --grace-period=0 --force
pod-killer: ## kill a random GPU pod every 20s for DUR seconds
	curl -s -XPOST $(ATK)/chaos/pod-killer -d '{"interval_s":20,"duration_s":$(DUR)}'; echo

# ------------------------------------------------------------- rollouts
eval: ## run the eval gate against v2 and register the result
	python3 evals/run_eval.py --version v1
	python3 evals/run_eval.py --version v2
canary-start: ## start a canary rollout of v2
	curl -s -XPOST $(CTL)/rollouts -d '{"candidate":"v2"}'; echo
canary-status:
	curl -s $(CTL)/rollouts/current; echo
canary-abort:
	curl -s -XPOST $(CTL)/rollouts/current/abort; echo

# ------------------------------------------------------------- humans & agents
console: ## run the React console on http://127.0.0.1:5173
	cd console && npm install --no-audit --no-fund && npm run dev
agent: ## run the AI SRE agent (host network, local Ollama)
	cd agent && ./run.sh

redeploy: ## rebuild + reload + restart one component (C=gateway|worker|attacker|controller)
	@if [ -f $(C)/go.mod ]; then cd $(C) && CGO_ENABLED=0 go build -o bin/$(C) ./cmd/$(C); fi
	docker build -q -t battlefield/$(C):dev $(C) >/dev/null
	kind load docker-image --name $(CLUSTER) battlefield/$(C):dev >/dev/null
	@if [ "$(C)" = worker ]; then kubectl -n $(NS) rollout restart statefulset/gpu deploy/gpu-canary; \
	 else kubectl -n $(NS) rollout restart deploy/$(C); fi
	@if [ "$(C)" = worker ]; then kubectl -n $(NS) rollout status statefulset/gpu --timeout=120s; \
	 else kubectl -n $(NS) rollout status deploy/$(C) --timeout=120s; fi

# ------------------------------------------------------------- edge fleet
fleet: ## fleet summary per ring
	@curl -s $(CTL)/fleet | python3 -m json.tool | head -40
edge-partition: ## cut 40% of devices off the network for DUR seconds
	@scripts/edge.sh /chaos/partition '{"fraction":0.4,"duration_s":$(DUR)}'
edge-network: ## add latency/loss to every device uplink
	@scripts/edge.sh /chaos/network '{"latency_ms":800,"loss":0.2}'
edge-reset: ## heal the edge network
	@scripts/edge.sh /chaos/reset
fleet-rollout: ## ring rollout of an edge model (VERSION=edge-v2-bad)
	curl -s -XPOST $(CTL)/fleet/rollouts -d '{"version":"$(VERSION)","hold_s":20}'; echo

test: ## unit tests (Go gateway, worker slot accounting)
	cd gateway && go test ./...
	cd controller && go vet ./...
	docker run --rm -v $(PWD)/worker:/w -w /w battlefield/worker:dev python test_slots.py
