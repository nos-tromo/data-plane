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
COMPOSE_ADMIN  := docker compose --env-file .env -f docker/compose.yaml -f docker/compose.admin.yaml
PROFILE_FLAG   := --profile $(PROFILE)
BACKUP_DIR     ?= ./backup/snapshots

# Direct database access, backups and restores are recorded in the
# data-protection audit trail by the admin-audit service (README: Audit
# trail): scripts/audit-admin.sh refuses to start one while admin-audit cannot
# record it. CASE_REF=<ref> and/or REASON=<text> state the purpose (make hands
# command-line variables to the recipe's environment); the operator is the
# sudo user, else the login user, e.g.
#   make db-shell CASE_REF=case-0001 REASON="verify a reported duplicate"
AUDITED = COMPOSE='$(COMPOSE)' PROFILE='$(PROFILE)' BACKUP_DIR='$(BACKUP_DIR)' ./scripts/audit-admin.sh

.PHONY: help network volumes pull bundle up up-dev up-admin stop down restart logs ps \
        health nuke db-shell qdrant-shell backup backup-neo4j backup-qdrant restore-neo4j restore-qdrant

help:
	@echo "data-plane — stateful services for chorus (Neo4j) + docint (Qdrant)."
	@echo
	@echo "Lifecycle:"
	@echo "  make network         create the external data-net if missing"
	@echo "  make volumes         create the external data volumes if missing"
	@echo "  make pull            pull all images from the registry"
	@echo "  make bundle          save images as a versioned airgap tarball ($(PROFILE))"
	@echo "  make up              start neo4j + qdrant ($(PROFILE) profile) + admin-audit"
	@echo "  make up-dev          like 'up', but publishes ports on the host"
	@echo "  make up-admin        like 'up', but binds 7474/7687/6333 to 127.0.0.1 for SSH-tunnelled admin access"
	@echo "  make stop            stop containers (keep them)"
	@echo "  make down            stop + remove containers (volumes preserved)"
	@echo "  make restart         down + up"
	@echo "  make nuke            DESTROY all data + volumes (interactive)"
	@echo
	@echo "Observability:"
	@echo "  make ps              service status"
	@echo "  make health          show health + uptime of each service; admin-audit can record"
	@echo "  make logs S=neo4j  tail logs for one service"
	@echo
	@echo "Direct access (recorded in the audit trail; always needs CASE_REF= or REASON=):"
	@echo "  make db-shell        cypher-shell session on Neo4j"
	@echo "  make qdrant-shell    shell with Qdrant's HTTP API (qdrant METHOD /path [JSON])"
	@echo
	@echo "Backup / restore (recorded; see backup/README.md for the full runbook):"
	@echo "  make backup          backup both DBs (needs CASE_REF= or REASON= by default)"
	@echo "  make backup-neo4j    just Neo4j (offline dump into $(BACKUP_DIR))"
	@echo "  make backup-qdrant   just Qdrant (snapshots into the qdrant-snapshots volume)"
	@echo "  make restore-neo4j DUMP=<file>                        REPLACE the neo4j database with a dump (asks first)"
	@echo "  make restore-qdrant COLLECTION=<name> SNAPSHOT=<file> REPLACE one collection with a snapshot (asks first)"
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

# Loopback-only dashboard ports for SSH-tunnelled admin access (see
# docker/compose.admin.yaml). A later plain `make up` recreates the
# containers without the ports — brief restart, volumes untouched.
up-admin: network volumes
	$(COMPOSE_ADMIN) $(PROFILE_FLAG) up --no-build -d

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
	@echo
	@$(COMPOSE) exec -T admin-audit python /app/admin_audit.py check && echo "admin-audit: can record"

logs:
ifndef S
	$(COMPOSE) $(PROFILE_FLAG) logs --tail=200 -f
else
	$(COMPOSE) $(PROFILE_FLAG) logs --tail=200 -f $(S)
endif

# ---- Direct access ---------------------------------------------------------
# Neo4j Community keeps no query log and Qdrant no access log, so these
# records are the only trail of a session: who opened it, when, why, how long
# it lasted. Both need a stated purpose, whatever AUDIT_REQUIRE_PURPOSE says.

db-shell:
	@$(AUDITED) shell neo4j -- ./scripts/neo4j_admin.sh shell

qdrant-shell:
	@$(AUDITED) shell qdrant -- ./scripts/qdrant_admin.sh shell

# ---- Backups ---------------------------------------------------------------
# These are intentionally minimal — see backup/README.md for retention,
# off-host copy, and verify-restore steps. Each is recorded as a disclosure
# to file:backup once it has run; under the default AUDIT_REQUIRE_PURPOSE
# (query,disclose) it needs CASE_REF= or REASON= — a scheduled backup passes
# one, e.g. REASON="nightly backup".

backup: backup-neo4j backup-qdrant

# Neo4j community edition has no online backup. We stop, dump, restart (the
# restart also runs when the dump fails). Confirm the brief downtime window
# is acceptable before scheduling.
backup-neo4j:
	@$(AUDITED) backup neo4j -- ./scripts/neo4j_admin.sh dump

# Qdrant supports online snapshots via its HTTP API. Iterates every
# collection. Snapshots land in the qdrant-snapshots volume; copying them
# off-host is a separate step (backup/README.md).
backup-qdrant:
	@$(AUDITED) backup qdrant -- ./scripts/qdrant_admin.sh snapshot

# ---- Restores --------------------------------------------------------------
# Each REPLACES live data, so it asks for confirmation, then is recorded as a
# modification once it has run (needs a purpose only if AUDIT_REQUIRE_PURPOSE
# lists modify).

restore-neo4j:
	@test -n "$(DUMP)" || { echo "usage: make restore-neo4j DUMP=<dump file in $(BACKUP_DIR)> [CASE_REF=...] [REASON=...]" >&2; exit 2; }
	@test -f "$(BACKUP_DIR)/$(DUMP)" || { echo "no dump $(DUMP) in $(BACKUP_DIR)" >&2; exit 2; }
	@echo "This REPLACES the neo4j database with $(DUMP); Neo4j stops while it loads."
	@read -p "Type 'restore' to confirm: " confirm && [ "$$confirm" = "restore" ] \
	  || { echo "aborted"; exit 1; }
	@$(AUDITED) restore neo4j 'file=$(DUMP)' -- ./scripts/neo4j_admin.sh load '$(DUMP)'

restore-qdrant:
	@test -n "$(COLLECTION)" && test -n "$(SNAPSHOT)" || { echo "usage: make restore-qdrant COLLECTION=<name> SNAPSHOT=<snapshot file> [CASE_REF=...] [REASON=...]" >&2; exit 2; }
	@echo "This REPLACES the Qdrant collection $(COLLECTION) with the snapshot $(SNAPSHOT)."
	@read -p "Type 'restore' to confirm: " confirm && [ "$$confirm" = "restore" ] \
	  || { echo "aborted"; exit 1; }
	@$(AUDITED) restore qdrant 'collection=$(COLLECTION)' 'snapshot=$(SNAPSHOT)' -- \
	  ./scripts/qdrant_admin.sh recover '$(COLLECTION)' '$(SNAPSHOT)'
