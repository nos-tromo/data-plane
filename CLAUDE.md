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

## Planning

For any non-trivial change (>1 file or any root-cause fix), present a plan and wait for approval BEFORE editing code. Do not start with Edit/Write on a fresh investigation.

## What data-plane is

The **state tier** of the nos-tromo federation: a Docker Compose project that owns the named volumes holding all graph and vector data, plus the two services that read/write them.

- **Neo4j** (community 5.x) — graph + native vector index for chorus
- **Qdrant** (1.18, `cpu`/`cuda` profile variants) — vector store for docint
- **admin-audit** (stdlib Python on the pinned python image) — records direct database access, backups and restores in the data-protection audit trail (README § Audit trail)

No application code and no Python venv. The whole repo is a `Makefile`, a `VERSION` file, three compose files under `docker/`, admin-audit's stdlib Python under `audit/` (`admin_audit.py`, the two audit modules vendored from `nos-tromo/.github`, and a `unittest` suite), the shell scripts under `scripts/` (the airgap bundler and its vendored `bundle-lib.sh`; `audit-admin.sh`, which records an operation through admin-audit; `admin-ports.sh`, which opens and closes the admin overlay's ports, recorded; `neo4j_admin.sh` and `qdrant_admin.sh`, the operator commands `audit-admin.sh` wraps; `qdrant_api.sh` and `qdrant_snapshot.sh`, which run inside the Qdrant container), a backup runbook under `backup/`, and CI under `.github/`. For how this tier slots into the wider workspace (inference vs state vs apps, the three external network seams `inference-net` / `data-net` / `edge-net`, bring-up order), see the parent `../CLAUDE.md`.

## Common commands

`PROFILE` is read from `.env` (default `cpu`); override on any target with `make <target> PROFILE=cuda` to use the GPU Qdrant image. Neo4j has no profile and always runs.

```bash
make network                  # create external data-net (idempotent; required once per host)
make volumes                  # create external data volumes (idempotent; required once per host)
make up                       # production shape — services on data-net only, NO host ports; closes an open admin-ports window (recorded)
make up-dev                   # layers docker/compose.override.yaml — publishes 7474/7687/6333/6334
make up-admin REASON="..."    # layers docker/compose.admin.yaml — 127.0.0.1:7474/7687/6333 on demand (SSH tunnel); needs a purpose, recorded
make down                     # stop; volumes preserved; closes an open admin-ports window (recorded)
make restart                  # down + up

make ps / make health         # service state / state + uptime, and whether admin-audit can record
make logs                     # tail all
make logs S=neo4j             # tail one service

make db-shell CASE_REF=case-0001 REASON="..."   # cypher-shell on Neo4j — recorded; always needs a purpose
make qdrant-shell REASON="..."                  # bash with `qdrant METHOD /path [JSON]` — recorded; always needs a purpose

make pull                     # pull images for BOTH profiles (cpu + cuda)
make bundle                   # airgap tarball from the latest release tag (current PROFILE)

make backup REASON="..."      # neo4j (offline dump) + qdrant (online snapshot) — recorded; purpose needed by default
make backup-neo4j             # neo4j only — briefly STOPS the service
make backup-qdrant            # qdrant only — online, no downtime
make restore-neo4j DUMP=<file>                     # REPLACE the neo4j database from a dump in backup/snapshots (asks)
make restore-qdrant COLLECTION=<c> SNAPSHOT=<file> # REPLACE one collection from a snapshot (asks)

python3 -m unittest discover -s audit/tests -t audit   # admin-audit and its scripts (synthetic data)

make nuke                     # DESTROY all volumes (interactive: type 'nuke' to confirm)
```

`make bundle` checks out the latest annotated release tag and bundles that (production). To bundle the current working tree instead, set `DATA_PLANE_VERSION_OVERRIDE=<version>` — this repo keeps a bespoke Makefile, so there is no separate `bundle-dev` target.

## Load-bearing invariants

### 1. data-plane is the only project that can destroy data

App compose files (`chorus/`, `docint/`) declare zero data-plane volumes, so their `docker compose down -v` cannot wipe graph or vector data. The blast radius of destroying this project's volumes is the entire dataset — that is concentrated in this one project on purpose. The volumes are declared `external`, so `docker compose down -v` cannot remove them either; `make nuke` therefore stops the stack with **both** profiles active (`--profile cpu --profile cuda down`, so it reaches the inactive Qdrant variant too) and then deletes each volume by name from the shared `VOLUMES` list in the `Makefile`, gated behind an interactive `nuke` confirmation that lists the volumes about to die. The recovery path is `backup/snapshots/` copied off-host on a schedule.

### 2. Production shape never publishes ports

`docker/compose.yaml` is production-shape: services `expose:` internally and join `data-net` by alias (`neo4j`, `qdrant`) but publish nothing to the host. Apps reach them at `bolt://neo4j:7687` and `http://qdrant:6333` purely over the network seam.

`docker/compose.override.yaml` is the dev overlay that publishes 7474/7687/6333/6334. **It is not auto-loaded** — it lives under `docker/` (not at the project root, where compose would pick it up implicitly), and the Makefile passes explicit `-f` flags. Only `make up-dev` layers it. The placement is intentional: in airgap production the host must not expose data-plane ports, and an override file at the default location would breach that boundary on a plain `compose up`.

`docker/compose.admin.yaml` is the only other port-publishing file. It binds 7474/7687/6333 to `127.0.0.1` only, is likewise not auto-loaded (`make up-admin`), and exists because edge-plane deliberately does not route the Neo4j Browser or Qdrant dashboard — admins reach them over an SSH tunnel to those loopback ports (README § Admin access). It is layered **on demand only**: production runs plain `make up` (deploy's bring-up too), and `make up-admin` needs a stated purpose and is recorded (invariant 4). `admin_audit.ADMIN_PORTS` lists its bindings for the record; a test keeps the two in step.

### 3. Airgap-first; no runtime fetches

- Every image is **digest-pinned** in `compose.yaml` — the two databases' and the python image admin-audit runs on (the same pin as edge-plane's auth-audit). Do not bump versions without also updating the digest.
- `NEO4J_PLUGINS` is empty in `.env.example` by design — populating it makes Neo4j pull plugin jars from the public CDN on first boot. Mirror plugin jars into the `neo4j-plugins` volume out-of-band instead.
- `scripts/bundle_images.sh` re-tags pulled-by-digest images (`docker tag name@digest name:tag`) before `docker save`, because Docker sometimes drops the `name:tag` binding when an image is pulled as `name:tag@digest` — and compose's `image: name:tag@digest` reference needs both to resolve from a loaded tarball.

### 4. Access that bypasses the apps is recorded by admin-audit

Neo4j Community keeps no query log and Qdrant no access log, so the make targets that open the stores directly (`db-shell`, `qdrant-shell`), copy their data out (`backup*`) or write it back (`restore-*`) are the only record of such access. Each runs its operation through `scripts/audit-admin.sh`: `admin_audit.py begin` admits it (exit 2 = refused: a missing purpose, recorded as `denied`, or a malformed one, unrecorded; exit 1 = cannot record), the operation runs, `admin_audit.py end` records the outcome (`ok`/`error`, exit status, duration, the facts the operation wrote to `$AUDIT_FACTS`). Fail closed: no record, no operation; a final record that cannot be written fails the target loudly. The operator is `$SUDO_USER`, else the login user; the purpose comes from `CASE_REF` / `REASON`.

- **Every record is a chain of its own, written by `docker compose exec` into the long-running `admin-audit`** (`AUDIT_OUTPUT=container`), never by a one-off container that could exit before the log collector sees it. obs-plane's verifier accepts data-plane's chains only from the emitter `data-plane/admin-audit` and only with `service: data-plane` (the compose project name, `SERVICE` in `admin_audit.py`). Renaming the service or the project means changing obs-plane's emitter list in the same change.
- **Purpose rules.** The shells always need one. Backups (`disclose`) and restores (`modify`) follow admin-audit's `AUDIT_REQUIRE_PURPOSE`, whose compose default is `${AUDIT_REQUIRE_PURPOSE-query,disclose}` — deliberately without the colon: `:-` would substitute the default for an explicit empty value too, so `AUDIT_REQUIRE_PURPOSE=` could not turn the requirement off. An unknown action fails admin-audit closed.
- **The audit calls read no stdin** (`</dev/null`): `docker compose exec` forwards stdin and would swallow a piped session's input. The purpose reaches them through valueless `-e` variables, never on a command line.
- **Only names and counts are recorded** — file names (never paths: a path can name a user), collection names, sizes, durations — never session content, file contents or credentials. `admin_audit.py` checks every key and value it accepts per operation.
- **The admin ports are a window, recorded at both ends** (`scripts/admin-ports.sh`). `make up-admin` writes `admin` / `db.ports.open` (`admin_audit.py begin ports`, no store; a purpose always required) before the overlay publishes anything, and refuses while that cannot be written or a window is already open. The open event's id and Unix time go to `.admin-ports` (gitignored, `0600`, `ADMIN_PORTS_STATE` overrides the path) — the only way to carry them to the close, which runs in a later make invocation. `make up` (after recreating the stores without ports) and `make down` (before removing them, never blocking the stop) write `admin` / `db.ports.close` with `parent_event_id` = the open event, `duration_s` from the stored time and `by=up|down`, then remove the file; a failed opening that published nothing closes at once with `by=up-admin`. When the close cannot be written the file stays, and the next `make up` records it (its duration then runs to that later close: an overstatement, never an understatement). `make health` reports from the containers' actual bindings, so ports published by hand show as open without an open record. The purpose is not stored: the close links to the opening instead.
- `audit/audit_event.py` and `audit/audit_runtime.py` are byte-identical copies of the hub's `configs/audit/`, checked by its `validate_audit_lib.py`: never edit them, never let a formatter touch them; re-vendor instead. `admin_audit.py` and its tests are clean under the hub's strict ruff (`configs/python-strict/ruff.toml`, pinned version) and strict pyrefly.

### 5. `data-net` is external and shared across compose projects

`data-net` is declared `external: true` (`name: ${DATA_NET:-data-net}`). `make network` creates it once per host; the app backends attach to the same network when they come up. The aliases `neo4j` and `qdrant` are how apps find these services across compose projects. It is the only network this project joins: the federation's other two seams — `inference-net` (apps ↔ vllm-service) and `edge-net` (gateway ↔ app frontends) — are **not** touched here.

## When editing the compose files

- The Neo4j healthcheck uses `cypher-shell` with parameter expansion on `$NEO4J_AUTH`. The `$$` is compose escaping — keep both `$$` so the container shell does the expansion, not compose itself. `make db-shell` takes the login from the same variable, handing it to cypher-shell through `NEO4J_USERNAME`/`NEO4J_PASSWORD` rather than its command line.
- Neo4j's `cap_add` ends with `KILL`: PID 1 is tini (root), the server runs as the `neo4j` user, and without `CAP_KILL` tini cannot forward `docker stop`'s SIGTERM — it dies with EPERM, the server is killed, and the offline dump after every stop refuses the store ("Active logical log detected"). With it the shutdown runs, and takes about 10 s even on a small store (the databases checkpoint within a second; stopping the Bolt server takes the rest), which is past Docker's default 10 s grace: hence `stop_grace_period: 2m`. At the deadline Docker SIGKILLs the server (exit 137), so do not shorten it.
- Qdrant's `expose:` and `volumes:` are YAML anchors (`&qdrant-ports`, `&qdrant-volumes`) shared by the `qdrant-cpu` and `qdrant-cuda` services. Edit one set; both consume it. Both write to the same `qdrant-storage` + `qdrant-snapshots` volumes, so switching `PROFILE` preserves data, but only one Qdrant runs at a time.
- Qdrant's `environment:` is the `&qdrant-env` anchor, shared by `qdrant-cpu` and `qdrant-cuda` the same way the ports and volumes are. It carries `QDRANT__SERVICE__API_KEY` as `${QDRANT_API_KEY:?…}`: required, like `NEO4J_PASSWORD`, so compose refuses every command without it, with a message saying how to generate one. Never let it render empty: an empty value is not "no auth" — Qdrant then answers every request that carries no key with 401, docint's included (what `${QDRANT_API_KEY:-}` did to every deployment that left the key unset, 0.3.5–0.4.0). `.env.example` leaves it empty on purpose, so a copied `.env` fails until a real key is set. Because the hub's infra-validation compose job stubs only `NEO4J_PASSWORD`/`OPENAI_API_KEY`, CI validates compose in this repo's `compose-config` job instead (every overlay × profile, from `.env.example` with a placeholder key, plus a check that the example alone is refused). `/readyz` is exempt from the key, so the healthcheck never needs it; `scripts/qdrant_snapshot.sh` and `scripts/qdrant_api.sh` read the key from the container's own environment, as `QDRANT__SERVICE__API_KEY` (the only name Qdrant reads), so rename the variable in both places or neither.
- The Qdrant images ship no `curl`/`wget`/`python3` (the slim cpu variant especially), but both do ship `bash` with `/dev/tcp` enabled. Everything that probes Qdrant therefore speaks raw HTTP over bash's built-in socket redirection: the shared `&qdrant-healthcheck` anchor (HTTP/1.0 `GET /readyz`, consumed by both variants), `scripts/qdrant_snapshot.sh` (piped into the container's `bash` by `make backup-qdrant`) and `scripts/qdrant_api.sh` (run with `bash -c` in the container: the `qdrant` helper of `make qdrant-shell`, and the recovery of `make restore-qdrant`). Don't switch these to an HTTP client without verifying it exists in the image first.
- `admin-audit` runs as `nobody` (65534) with a read-only root, no network and no volumes: it mounts `../audit` and `../VERSION` read-only and writes only its stdout. Every `docker compose exec` into it runs as the same user, which is what lets an exec'd process write to PID 1's stdout (`/proc/1/fd/1`) under the shared lock on its tmpfs `/tmp`.
- The `volumes:` block in `compose.yaml` declares its six named volumes `external`, so compose neither creates nor destroys them — `make volumes` creates them and `make nuke` deletes them by name. Those names must stay in sync with the `VOLUMES` list in the `Makefile`; adding or renaming a volume means editing both files.

## Backups: what the runbook assumes

Full procedure in `backup/README.md`. The constraints to know when touching the Makefile:

- **Neo4j community edition has no online backup.** `scripts/neo4j_admin.sh dump` (`backup-neo4j`) stops Neo4j, runs `neo4j-admin database dump` in a one-shot container with `backup/snapshots/` mounted, and starts it again — also when the dump failed. Downtime scales with dataset size. Adding online backup means switching to Enterprise — that is an ADR-level decision, not a Makefile tweak. `load` (`restore-neo4j`) mounts the chosen dump as `/backup/neo4j.dump`, because `neo4j-admin database load` reads `<database>.dump` from `--from-path`.
- **Qdrant snapshots are online**, via `POST /collections/<name>/snapshots`. The script iterates `GET /collections` and reports what it wrote (collection names, total size) on stdout for the audit record; its progress goes to stderr. Snapshots land *inside* the `qdrant-snapshots` volume — copying them off-host is a separate step (`docker compose cp`), documented in `backup/README.md`. `restore-qdrant` streams a host copy back in through `docker compose exec` instead of `docker compose cp`, which keeps the host file's owner and mode: Qdrant runs as root without capabilities and cannot open a file it does not own.

## Git & PR Workflow

- Never commit directly to `main`; always branch (`feat/`, `fix/`) and open a PR.
- Never create a NEW PR when an existing PR for the work is open — push additional commits to that branch.
- Release order is strict: bump VERSION file -> commit -> tag. Never tag before the VERSION bump.
- Use single, non-compound shell commands for `gh` operations (no `&&` chains); if `gh pr merge` is blocked, fall back to the GitHub MCP merge tool.

## Verification

Before claiming a check is green, run the actual command and paste the output. `git ls-files` does not cover untracked files — use `pre-commit run --all-files`. After opening a PR, confirm CI actually triggered on the latest push before declaring done.

## Communication Style

Keep changes minimal and scoped. Do not add explanatory code comments for trivial or self-evident changes. Do not overwrite existing test files with Write — use Edit to append or modify tests.
