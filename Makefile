# One command per step. `make help` lists them.
SHELL := /bin/bash
CLUSTER := stateless-agents
CTX := kind-$(CLUSTER)
K := kubectl --context $(CTX)
DRIVE := uv run --quiet demo/drive.py
N ?= 3

.PHONY: help up images load deploy down reset board chat scale \
        sessions-in-memory sessions-in-postgres followups \
        lease-off lease-on double-tap stale-writer \
        talkers roll drain-test settle shell-entrypoint exec-entrypoint \
        gate-off gate-on crowd act1 act2 act3 act4 real

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | column -t -s $$'\t'

# --- Setup -------------------------------------------------------------------

up: ## create the cluster, build the images and start everything
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config k8s/kind.yaml
	@$(MAKE) --no-print-directory images load deploy

images:
	docker build -q -t stateless-agents/agent:dev agent
	docker build -q -t stateless-agents/fakemodel:dev fakemodel

load:
	kind load docker-image --name $(CLUSTER) stateless-agents/agent:dev stateless-agents/fakemodel:dev

deploy:
	$(K) apply -f k8s/app.yaml
	$(K) rollout status deploy/postgres deploy/redis deploy/fakemodel deploy/agent --timeout=180s
	@echo "Ready. Open the board in another terminal: make board"

down: ## delete the cluster
	kind delete cluster --name $(CLUSTER)

reset: ## back to the start: one pod, every switch on, a clean board
	$(K) scale deploy/agent --replicas=1
	@$(DRIVE) reset
	$(K) rollout status deploy/agent --timeout=120s

board: ## the live board (run it in its own terminal)
	@uv run --quiet demo/board.py

chat: ## talk to the agent yourself
	@$(DRIVE) chat

scale: ## run N agent pods: make scale N=3
	$(K) scale deploy/agent --replicas=$(N)
	$(K) rollout status deploy/agent --timeout=120s
	$(K) get pods -l app=agent -o wide

pods:
	$(K) get pods -o wide

# --- Act 1: context outlives the pod -----------------------------------------

sessions-in-memory: ## each pod keeps conversations in its own memory
	@$(DRIVE) flags sessions=memory
sessions-in-postgres: ## every pod reads and writes conversations in Postgres
	@$(DRIVE) flags sessions=postgres
followups: ## ten customers, two messages each
	@$(DRIVE) followups --n 10

# --- Act 2: one writer per conversation --------------------------------------

lease-off: ## no lease: any pod may work on any conversation at any time
	@$(DRIVE) flags lease=off
lease-on: ## one turn per conversation at a time, with a fence on every write
	@$(DRIVE) flags lease=on
double-tap: ## two 'refund my order' messages at the same moment
	@$(DRIVE) double-tap
stale-writer: ## a pod freezes past its lease, then wakes up and tries to refund
	@$(DRIVE) stale-writer

# --- Act 3: let a pod finish before it goes ----------------------------------

shell-entrypoint: ## start the server through a shell (it won't hear SIGTERM)
	$(K) patch deploy/agent --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["/app/start-shell.sh"]}]'
	$(K) rollout status deploy/agent --timeout=180s
	@$(MAKE) -s settle
exec-entrypoint: ## exec the server, so it hears SIGTERM and drains
	$(K) patch deploy/agent --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["/app/start.sh"]}]'
	$(K) rollout status deploy/agent --timeout=180s
	@$(MAKE) -s settle
settle: ## wait until the old pods are gone (a shell-started pod takes the full 30 s)
	@echo "waiting for the old pods to go..."
	@while $(K) get pods -l app=agent --no-headers | grep -q Terminating; do sleep 1; done
	@$(K) get pods -l app=agent
talkers: ## six customers asking long questions for a minute
	@$(DRIVE) talkers --n 6 --seconds 60
roll: ## restart every agent pod, the way a deploy does
	$(K) rollout restart deploy/agent
drain-test: ## six customers asking long questions; 8 s in, every pod restarts
	@$(DRIVE) talkers --n 6 --seconds 50 & sleep 8; $(MAKE) -s roll; wait

# --- Act 4: manage what you can't scale --------------------------------------

gate-off: ## every pod calls the model whenever it likes
	@$(DRIVE) flags gate=off
gate-on: ## one first-come line in front of the model, shared by every pod
	@$(DRIVE) flags gate=on
crowd: ## sixteen customers, three messages each, against a quota of 4 calls at once
	@$(DRIVE) model max_concurrent=4 > /dev/null
	@$(DRIVE) crowd --n 16 --turns 3

# --- The acts, start to finish, for anyone replaying the demo ----------------

act1: reset ## context outlives the pod
	@$(MAKE) -s scale N=3
	@$(MAKE) -s sessions-in-memory
	@$(MAKE) -s followups
	@$(MAKE) -s sessions-in-postgres
	@$(MAKE) -s followups
act2: reset ## one writer per conversation
	@$(MAKE) -s scale N=3
	@$(MAKE) -s lease-off
	@$(MAKE) -s double-tap
	@$(MAKE) -s lease-on
	@$(MAKE) -s double-tap
	@$(MAKE) -s stale-writer
act3: reset ## let a pod finish before it goes
	@$(MAKE) -s scale N=3
	@$(MAKE) -s shell-entrypoint
	@$(MAKE) -s drain-test
	@$(MAKE) -s exec-entrypoint
	@$(MAKE) -s drain-test
act4: reset ## manage what you can't scale
	@$(MAKE) -s scale N=3
	@$(MAKE) -s gate-off
	@$(MAKE) -s crowd
	@$(MAKE) -s gate-on
	@$(MAKE) -s crowd
	@$(MAKE) -s scale N=6
	@$(MAKE) -s crowd

real: ## swap the fake model for Gemini: GEMINI_API_KEY=... make real
	@test -n "$(GEMINI_API_KEY)" || (echo "set GEMINI_API_KEY first" && exit 1)
	$(K) set env deploy/agent GOOGLE_GEMINI_BASE_URL- GOOGLE_API_KEY=$(GEMINI_API_KEY)
	$(K) rollout status deploy/agent --timeout=180s
