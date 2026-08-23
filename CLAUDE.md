# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## What data-plane is

The **state tier** of the nos-tromo federation: a Docker Compose project that owns the named volumes holding all graph and vector data, plus the two services that read/write them.

- **Neo4j** (community 5.x) — graph + native vector index for chorus
- **Qdrant** (1.18, `cpu`/`cuda` profile variants) — vector store for docint

No application code. No Python venv, no test suite, no linter. The whole repo is a `Makefile`, a `VERSION` file, two compose files under `docker/`, three shell scripts under `scripts/` (the airgap bundler, its vendored `bundle-lib.sh`, and `qdrant_snapshot.sh`), a backup runbook under `backup/`, and CI under `.github/`. For how this tier slots into the wider workspace (inference vs state vs apps, the three external network seams `inference-net` / `data-net` / `edge-net`, bring-up order), see the parent `../CLAUDE.md`.

## Common commands

`PROFILE` is read from `.env` (default `cpu`); override on any target with `make <target> PROFILE=cuda` to use the GPU Qdrant image. Neo4j has no profile and always runs.

```bash
make network                  # create external data-net (idempotent; required once per host)
make volumes                  # create external data volumes (idempotent; required once per host)
make up                       # production shape — services on data-net only, NO host ports
make up-dev                   # layers docker/compose.override.yaml — publishes 7474/7687/6333/6334
make down                     # stop; volumes preserved
make restart                  # down + up

make ps / make health         # service state / state + uptime
make logs                     # tail all
make logs S=neo4j             # tail one service

make pull                     # pull images for BOTH profiles (cpu + cuda)
make bundle                   # airgap tarball from the latest release tag (current PROFILE)

make backup                   # neo4j (offline dump) + qdrant (online snapshot)
make backup-neo4j             # neo4j only — briefly STOPS the service
make backup-qdrant            # qdrant only — online, no downtime

make nuke                     # DESTROY all volumes (interactive: type 'nuke' to confirm)
```

`make bundle` checks out the latest annotated release tag and bundles that (production). To bundle the current working tree instead, set `DATA_PLANE_VERSION_OVERRIDE=<version>` — this repo keeps a bespoke Makefile, so there is no separate `bundle-dev` target.

## Load-bearing invariants

### 1. data-plane is the only project that can destroy data

App compose files (`chorus/`, `docint/`) declare zero data-plane volumes, so their `docker compose down -v` cannot wipe graph or vector data. The blast radius of destroying this project's volumes is the entire dataset — that is concentrated in this one project on purpose. The volumes are declared `external`, so `docker compose down -v` cannot remove them either; `make nuke` therefore stops the stack with **both** profiles active (`--profile cpu --profile cuda down`, so it reaches the inactive Qdrant variant too) and then deletes each volume by name from the shared `VOLUMES` list in the `Makefile`, gated behind an interactive `nuke` confirmation that lists the volumes about to die. The recovery path is `backup/snapshots/` copied off-host on a schedule.

### 2. Production shape never publishes ports

`docker/compose.yaml` is production-shape: services `expose:` internally and join `data-net` by alias (`neo4j`, `qdrant`) but publish nothing to the host. Apps reach them at `bolt://neo4j:7687` and `http://qdrant:6333` purely over the network seam.

`docker/compose.override.yaml` is the dev overlay that publishes 7474/7687/6333/6334. **It is not auto-loaded** — it lives under `docker/` (not at the project root, where compose would pick it up implicitly), and the Makefile passes explicit `-f` flags. Only `make up-dev` layers it. The placement is intentional: in airgap production the host must not expose data-plane ports, and an override file at the default location would breach that boundary on a plain `compose up`.

### 3. Airgap-first; no runtime fetches

- Both service images are **digest-pinned** in `compose.yaml`. Do not bump versions without also updating the digest.
- `NEO4J_PLUGINS` is empty in `.env.example` by design — populating it makes Neo4j pull plugin jars from the public CDN on first boot. Mirror plugin jars into the `neo4j-plugins` volume out-of-band instead.
- `scripts/bundle_images.sh` re-tags pulled-by-digest images (`docker tag name@digest name:tag`) before `docker save`, because Docker sometimes drops the `name:tag` binding when an image is pulled as `name:tag@digest` — and compose's `image: name:tag@digest` reference needs both to resolve from a loaded tarball.

### 4. `data-net` is external and shared across compose projects

`data-net` is declared `external: true` (`name: ${DATA_NET:-data-net}`). `make network` creates it once per host; the app backends attach to the same network when they come up. The aliases `neo4j` and `qdrant` are how apps find these services across compose projects. It is the only network this project joins: the federation's other two seams — `inference-net` (apps ↔ vllm-service) and `edge-net` (gateway ↔ app frontends) — are **not** touched here.

## When editing the compose files

- The Neo4j healthcheck uses `cypher-shell` with parameter expansion on `$NEO4J_AUTH`. The `$$` is compose escaping — keep both `$$` so the container shell does the expansion, not compose itself.
- Qdrant's `expose:` and `volumes:` are YAML anchors (`&qdrant-ports`, `&qdrant-volumes`) shared by the `qdrant-cpu` and `qdrant-cuda` services. Edit one set; both consume it. Both write to the same `qdrant-storage` + `qdrant-snapshots` volumes, so switching `PROFILE` preserves data, but only one Qdrant runs at a time.
- The Qdrant images ship no `curl`/`wget`/`python3` (the slim cpu variant especially), but both do ship `bash` with `/dev/tcp` enabled. Everything that probes Qdrant therefore speaks raw HTTP over bash's built-in socket redirection: the shared `&qdrant-healthcheck` anchor (HTTP/1.0 `GET /readyz`, consumed by both variants) and `scripts/qdrant_snapshot.sh` (piped into the container's `bash` by `make backup-qdrant`). Don't switch these to an HTTP client without verifying it exists in the image first.
- The `volumes:` block in `compose.yaml` declares its six named volumes `external`, so compose neither creates nor destroys them — `make volumes` creates them and `make nuke` deletes them by name. Those names must stay in sync with the `VOLUMES` list in the `Makefile`; adding or renaming a volume means editing both files.

## Backups: what the runbook assumes

Full procedure in `backup/README.md`. The constraints to know when touching the Makefile:

- **Neo4j community edition has no online backup.** `backup-neo4j` stops Neo4j, runs `neo4j-admin database dump` in a one-shot container with `backup/snapshots/` mounted, restarts. Downtime scales with dataset size. Adding online backup means switching to Enterprise — that is an ADR-level decision, not a Makefile tweak.
- **Qdrant snapshots are online**, via `POST /collections/<name>/snapshots`. The script iterates `GET /collections`. Snapshots land *inside* the `qdrant-snapshots` volume — copying them off-host is a separate step (`docker compose cp`), documented in `backup/README.md`.
