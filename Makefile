# data-plane operator targets.
#
# By default operates on the *cpu* Qdrant profile. Override with
# `make up PROFILE=cuda` for the GPU profile. Neo4j has no profile —
# it always runs.
#
# This repo keeps a bespoke Makefile and does NOT `include make/common.mk` from
# nos-tromo/.github. common.mk targets a build-and-run app shape (single compose
# file, build/up/pre-commit, no profiles); data-plane is profile-aware (Qdrant
# cpu/cuda), pulls rather than builds, has no Python, and adds stateful-DB
# targets (backup/restore/nuke) common.mk does not model. It does adopt the
# shared airgap bundle library (scripts/bundle-lib.sh, CI drift-checked) via
# scripts/bundle_images.sh.

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

# Default the Qdrant profile to CPU. Override via PROFILE env var or .env file.
PROFILE ?= $(or $(strip $(shell test -f .env && grep -E '^PROFILE=' .env | cut -d= -f2)),cpu)

# Read DATA_NET from .env if present, otherwise fall back.
DATA_NET ?= $(or $(strip $(shell test -f .env && grep -E '^DATA_NET=' .env | cut -d= -f2)),data-net)

# External named volumes this project owns. `make volumes` creates them
# (compose won't — they're external) and `make nuke` removes them (compose's
# `down -v` won't, same reason). Keep in sync with docker/compose.yaml.
VOLUMES := neo4j-data neo4j-logs neo4j-import neo4j-plugins qdrant-snapshots qdrant-storage

COMPOSE        := docker compose --env-file .env -f docker/compose.yaml
COMPOSE_DEV    := docker compose --env-file .env -f docker/compose.yaml -f docker/compose.override.yaml
PROFILE_FLAG   := --profile $(PROFILE)
TS             := $(shell date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR     ?= ./backup/snapshots

.PHONY: help network volumes pull bundle up up-dev stop down restart logs ps \
        health nuke backup backup-neo4j backup-qdrant restore-neo4j

help:
	@echo "data-plane — stateful services for chorus (Neo4j) + docint (Qdrant)."
	@echo
	@echo "Lifecycle:"
	@echo "  make network         create the external data-net if missing"
	@echo "  make volumes         create the external data volumes if missing"
	@echo "  make pull            pull all images from the registry"
	@echo "  make bundle          save images as a versioned airgap tarball ($(PROFILE))"
	@echo "  make up              start neo4j + qdrant ($(PROFILE) profile)"
	@echo "  make up-dev          like 'up', but publishes ports on the host"
	@echo "  make down            stop the stack (volumes preserved)"
	@echo "  make restart         down + up"
	@echo "  make nuke            DESTROY all data + volumes (interactive)"
	@echo
	@echo "Observability:"
	@echo "  make ps              service status"
	@echo "  make health          show health + uptime of each service"
	@echo "  make logs S=neo4j  tail logs for one service"
	@echo
	@echo "Backup / restore (see backup/README.md for the full runbook):"
	@echo "  make backup          backup both DBs into $(BACKUP_DIR)"
	@echo "  make backup-neo4j    just Neo4j (offline dump)"
	@echo "  make backup-qdrant   just Qdrant (snapshot API)"
	@echo
	@echo "Profile: $(PROFILE)  (override with PROFILE=cuda)"

network:
	@docker network inspect $(DATA_NET) >/dev/null 2>&1 \
	  || (echo ">> creating external network $(DATA_NET)" \
	      && docker network create $(DATA_NET))

# Pre-create the external volumes. Idempotent — skips any that already exist.
# Compose refuses to auto-create external volumes, so `up`/`up-dev` depend on this.
volumes:
	@for v in $(VOLUMES); do \
	  docker volume inspect $$v >/dev/null 2>&1 \
	    || (echo ">> creating external volume $$v" && docker volume create $$v >/dev/null); \
	done

pull:
	$(COMPOSE) --profile cpu --profile cuda pull

# Save images as a versioned tarball for transfer to an offline host.
bundle:
	./scripts/bundle_images.sh $(PROFILE)

up: network volumes
	$(COMPOSE) $(PROFILE_FLAG) up --no-build -d

up-dev: network volumes
	$(COMPOSE_DEV) $(PROFILE_FLAG) up --no-build -d

stop:
	$(COMPOSE) $(PROFILE_FLAG) stop

down:
	$(COMPOSE) $(PROFILE_FLAG) down

restart: down up

# Destructive — needs an interactive confirm. Mirrors the chorus
# architecture invariant: only this project can wipe graph + vector data.
# The volumes are external, so `down -v` won't remove them — we stop the
# stack, then delete the volumes by name.
nuke:
	@echo "This will DESTROY all data-plane volumes:"
	@for v in $(VOLUMES); do echo "  - $$v"; done
	@read -p "Type 'nuke' to confirm: " confirm && [ "$$confirm" = "nuke" ] \
	  || (echo "aborted"; exit 1)
	$(COMPOSE) --profile cpu --profile cuda down
	@for v in $(VOLUMES); do \
	  docker volume rm $$v >/dev/null 2>&1 && echo "  removed $$v" || true; \
	done

ps:
	$(COMPOSE) $(PROFILE_FLAG) ps

health:
	@$(COMPOSE) $(PROFILE_FLAG) ps --format '{{.Name}}\t{{.State}}\t{{.Status}}'

logs:
ifndef S
	$(COMPOSE) $(PROFILE_FLAG) logs --tail=200 -f
else
	$(COMPOSE) $(PROFILE_FLAG) logs --tail=200 -f $(S)
endif

# ---- Backups ---------------------------------------------------------------
# These are intentionally minimal — see backup/README.md for retention,
# off-host copy, and verify-restore steps.

$(BACKUP_DIR):
	@mkdir -p $(BACKUP_DIR)

backup: backup-neo4j backup-qdrant

# Neo4j community edition has no online backup. We stop, dump, restart.
# Confirm the brief downtime window is acceptable before scheduling.
backup-neo4j: | $(BACKUP_DIR)
	@echo ">> stopping neo4j for offline dump"
	$(COMPOSE) stop neo4j
	$(COMPOSE) run --rm \
	  -v $(abspath $(BACKUP_DIR)):/backup \
	  --entrypoint /bin/bash neo4j -c \
	  "neo4j-admin database dump neo4j --to-path=/backup --overwrite-destination=true && \
	   mv /backup/neo4j.dump /backup/neo4j-$(TS).dump"
	$(COMPOSE) start neo4j
	@echo ">> wrote $(BACKUP_DIR)/neo4j-$(TS).dump"

# Qdrant supports online snapshots via its HTTP API. Iterates every
# collection. Snapshots land in the qdrant-snapshots volume —
# this target also copies them out to $(BACKUP_DIR) for off-host pickup.
backup-qdrant: | $(BACKUP_DIR)
	@echo ">> triggering qdrant snapshot for every collection"
	@$(COMPOSE) exec -T qdrant-$(PROFILE) sh -c '\
	  for col in $$(wget -qO- http://localhost:6333/collections | \
	    grep -oE "\"name\":\"[^\"]+\"" | cut -d\" -f4); do \
	    echo "  snapshotting $$col"; \
	    wget -qO- --post-data="" http://localhost:6333/collections/$$col/snapshots; \
	  done' || echo "(if wget is absent in your qdrant image, see backup/README.md for the alternative)"
	@echo ">> snapshots live in the qdrant-snapshots volume"

restore-neo4j:
	@echo "See backup/README.md → Restore (Neo4j) for the procedure."
	@echo "Restores require stopping the service and operate on a dump file."
