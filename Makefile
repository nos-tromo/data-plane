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

# Backups are encrypted with age to the public keys in this file, one per line
# (backup/README.md: Encryption); without it every backup refuses to run.
# Override via BACKUP_RECIPIENTS in .env or on the command line.
BACKUP_RECIPIENTS ?= $(or $(strip $(shell test -f .env && grep -E '^BACKUP_RECIPIENTS=' .env | cut -d= -f2-)),./backup/recipients.txt)

# Backups are short-lived restore points, not an archive: after every backup
# that succeeded, the store's backups older than this many days (1 to 365) are
# deleted, recorded (backup/README.md: Retention). Override via
# BACKUP_RETENTION_DAYS in .env or on the command line.
BACKUP_RETENTION_DAYS ?= $(or $(strip $(shell test -f .env && grep -E '^BACKUP_RETENTION_DAYS=' .env | cut -d= -f2-)),30)
# A new period deletes nothing for this many days (1 to 365); backup/README.md: Retention.
RETENTION_NOTICE_DAYS ?= $(or $(strip $(shell test -f .env && grep -E '^RETENTION_NOTICE_DAYS=' .env | cut -d= -f2-)),30)

# Direct database access, backups and restores are recorded in the
# data-protection audit trail by the admin-audit service (README: Audit
# trail): scripts/audit-admin.sh refuses to start one while admin-audit cannot
# record it. CASE_REF=<ref> and/or REASON=<text> state the purpose (make hands
# command-line variables to the recipe's environment); the operator is the
# sudo user, else the login user, e.g.
#   make db-shell CASE_REF=case-0001 REASON="verify a reported duplicate"
AUDITED = COMPOSE='$(COMPOSE)' PROFILE='$(PROFILE)' BACKUP_DIR='$(BACKUP_DIR)' BACKUP_RECIPIENTS='$(BACKUP_RECIPIENTS)' BACKUP_RETENTION_DAYS='$(BACKUP_RETENTION_DAYS)' RETENTION_NOTICE_DAYS='$(RETENTION_NOTICE_DAYS)' ./scripts/audit-admin.sh
# Checks the retention period before a backup or a pruning asks for anything.
RETENTION = BACKUP_RETENTION_DAYS='$(BACKUP_RETENTION_DAYS)' RETENTION_NOTICE_DAYS='$(RETENTION_NOTICE_DAYS)' ./scripts/backup_prune.sh
# Checks that a backup can be encrypted before admin-audit is asked for anything.
ENCRYPTION = BACKUP_RECIPIENTS='$(BACKUP_RECIPIENTS)' ./scripts/backup_age.sh
# The admin overlay's loopback ports open only on demand, and both their
# opening and their close are recorded (scripts/admin-ports.sh, README: Admin
# access). The open window is kept in .admin-ports between the two.
PORTS = COMPOSE='$(COMPOSE)' ./scripts/admin-ports.sh

.PHONY: help network volumes pull bundle up up-dev up-admin stop down restart logs ps \
        health nuke db-shell qdrant-shell backup backup-neo4j backup-qdrant backup-prune restore-neo4j restore-qdrant

help:
	@echo "data-plane — stateful services for chorus (Neo4j) + docint (Qdrant)."
	@echo
	@echo "Lifecycle:"
	@echo "  make network         create the external data-net if missing"
	@echo "  make volumes         create the external data volumes if missing"
	@echo "  make pull            pull all images from the registry"
	@echo "  make bundle          save images as a versioned airgap tarball ($(PROFILE))"
	@echo "  make up              start neo4j + qdrant ($(PROFILE) profile) + admin-audit; closes the admin ports"
	@echo "  make up-dev          like 'up', but publishes ports on the host"
	@echo "  make up-admin        like 'up', plus 127.0.0.1:7474/7687/6333 for SSH-tunnelled dashboards;"
	@echo "                       needs CASE_REF= or REASON=, recorded until make up (or down) closes them"
	@echo "  make stop            stop containers (keep them)"
	@echo "  make down            stop + remove containers (volumes preserved)"
	@echo "  make restart         down + up"
	@echo "  make nuke            DESTROY all data + volumes (interactive)"
	@echo
	@echo "Observability:"
	@echo "  make ps              service status"
	@echo "  make health          show health + uptime of each service, the admin ports; admin-audit can record"
	@echo "  make logs S=neo4j  tail logs for one service"
	@echo
	@echo "Direct access (recorded in the audit trail; always needs CASE_REF= or REASON=):"
	@echo "  make db-shell        cypher-shell session on Neo4j"
	@echo "  make qdrant-shell    shell with Qdrant's HTTP API (qdrant METHOD /path [JSON])"
	@echo
	@echo "Backup / restore (recorded; see backup/README.md for the full runbook):"
	@echo "  make backup          backup both DBs, encrypted with age (needs CASE_REF= or REASON= by default)"
	@echo "  make backup-neo4j    just Neo4j (offline dump, encrypted into $(BACKUP_DIR))"
	@echo "  make backup-qdrant   just Qdrant (online snapshots, encrypted into $(BACKUP_DIR))"
	@echo "                       both encrypt to the public keys in $(BACKUP_RECIPIENTS)"
	@echo "  make backup-prune    delete backups older than BACKUP_RETENTION_DAYS ($(BACKUP_RETENTION_DAYS)); every backup does it too"
	@echo "  make restore-neo4j DUMP=<file> IDENTITY=<key file>    REPLACE the neo4j database with a dump (asks first)"
	@echo "  make restore-qdrant COLLECTION=<name> SNAPSHOT=<file> IDENTITY=<key file>"
	@echo "                       REPLACE one collection with a snapshot (asks first)"
	@echo "                       both decrypt with the age identity given for that run only"
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

# Production shape. Recreating the stores without the admin ports ends an
# open admin-ports window, which is then recorded as closed.
up: network volumes
	$(COMPOSE) $(PROFILE_FLAG) up --no-build -d
	@$(PORTS) close up

up-dev: network volumes
	$(COMPOSE_DEV) $(PROFILE_FLAG) up --no-build -d

# Loopback-only dashboard ports for SSH-tunnelled admin access (see
# docker/compose.admin.yaml), on demand: always needs a stated purpose, and
# admin / db.ports.open is recorded before anything is published. A later
# plain `make up` recreates the containers without the ports (brief restart,
# volumes untouched) and records the close.
up-admin: network volumes
	@$(PORTS) open -- $(COMPOSE_ADMIN) $(PROFILE_FLAG) up --no-build -d

stop:
	$(COMPOSE) $(PROFILE_FLAG) stop

# An open admin-ports window ends with the containers: its close is recorded
# first, while admin-audit still runs. That never holds up the stop; if it
# cannot be recorded, the next make up records it.
down:
	-@$(PORTS) close down
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
	@$(PORTS) status
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
# off-host copy, and verify-restore steps. Each is encrypted with age on its
# way out of the store, to the public keys in BACKUP_RECIPIENTS, and refuses
# before doing anything while it cannot be. Each is recorded as a disclosure
# to file:backup once it has run; under the default AUDIT_REQUIRE_PURPOSE
# (query,disclose) it needs CASE_REF= or REASON= — a scheduled backup passes
# one, e.g. REASON="scheduled backup".

backup: backup-neo4j backup-qdrant

# Neo4j community edition has no online backup. We stop, dump, restart (the
# restart also runs when the dump fails). Confirm the brief downtime window
# is acceptable before scheduling.
backup-neo4j:
	@$(RETENTION) check
	@$(ENCRYPTION) recipients
	@$(AUDITED) backup neo4j -- ./scripts/neo4j_admin.sh dump
	@$(AUDITED) prune neo4j -- ./scripts/backup_prune.sh neo4j

# Qdrant supports online snapshots via its HTTP API. Iterates every
# collection; each snapshot leaves the qdrant-snapshots volume encrypted, for
# BACKUP_DIR, and is deleted there. Copying BACKUP_DIR off-host is a separate
# step (backup/README.md).
backup-qdrant:
	@$(RETENTION) check
	@$(ENCRYPTION) recipients
	@$(AUDITED) backup qdrant -- ./scripts/qdrant_admin.sh snapshot
	@$(AUDITED) prune qdrant -- ./scripts/backup_prune.sh qdrant

# The pruning on its own, for both stores (each recorded as delete /
# backup.prune.<store>).
backup-prune:
	@$(RETENTION) check
	@$(AUDITED) prune neo4j -- ./scripts/backup_prune.sh neo4j
	@$(AUDITED) prune qdrant -- ./scripts/backup_prune.sh qdrant

# ---- Restores --------------------------------------------------------------
# Each takes an encrypted backup from BACKUP_DIR and IDENTITY=<file>, the age
# identity it was encrypted to, for this run only (never stored or recorded),
# and decrypts by streaming; both are checked before anything is asked. Each
# REPLACES live data, so it asks for confirmation, then is recorded as a
# modification once it has run (needs a purpose only if AUDIT_REQUIRE_PURPOSE
# lists modify).

restore-neo4j:
	@test -n "$(DUMP)" || { echo "usage: make restore-neo4j DUMP=<encrypted dump in $(BACKUP_DIR)> IDENTITY=<age identity file> [CASE_REF=...] [REASON=...]" >&2; exit 2; }
	@test -f "$(BACKUP_DIR)/$(DUMP)" || { echo "no dump $(DUMP) in $(BACKUP_DIR)" >&2; exit 2; }
	@$(ENCRYPTION) identity '$(BACKUP_DIR)/$(DUMP)'
	@echo "This REPLACES the neo4j database with $(DUMP); Neo4j stops while it loads."
	@read -p "Type 'restore' to confirm: " confirm && [ "$$confirm" = "restore" ] \
	  || { echo "aborted"; exit 1; }
	@$(AUDITED) restore neo4j 'file=$(DUMP)' -- ./scripts/neo4j_admin.sh load '$(DUMP)'

restore-qdrant:
	@test -n "$(COLLECTION)" && test -n "$(SNAPSHOT)" || { echo "usage: make restore-qdrant COLLECTION=<name> SNAPSHOT=<encrypted snapshot in $(BACKUP_DIR)> IDENTITY=<age identity file> [CASE_REF=...] [REASON=...]" >&2; exit 2; }
	@test -f "$(BACKUP_DIR)/$(SNAPSHOT)" || { echo "no snapshot $(SNAPSHOT) in $(BACKUP_DIR)" >&2; exit 2; }
	@$(ENCRYPTION) identity '$(BACKUP_DIR)/$(SNAPSHOT)'
	@echo "This REPLACES the Qdrant collection $(COLLECTION) with the snapshot $(SNAPSHOT)."
	@read -p "Type 'restore' to confirm: " confirm && [ "$$confirm" = "restore" ] \
	  || { echo "aborted"; exit 1; }
	@$(AUDITED) restore qdrant 'collection=$(COLLECTION)' 'snapshot=$(SNAPSHOT)' -- \
	  ./scripts/qdrant_admin.sh recover '$(COLLECTION)' '$(SNAPSHOT)'
