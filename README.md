# data-plane

Stateful services for the nos-tromo apps. This project owns the named
Docker volumes for graph and vector data; the apps (chorus, docint) stay
stateless and disposable.

## What lives here

| Compose service | Used by | Network alias on `data-net` | Why |
|---|---|---|---|
| `neo4j` | chorus | `neo4j` | Graph + native vectors |
| `qdrant-cpu` / `qdrant-cuda` | docint | `qdrant` | Document vector store — one variant runs at a time, picked by `PROFILE` |
| `admin-audit` | operators (make targets) | — (no network) | Records direct database access, backups and restores in the audit trail ([Audit trail](#audit-trail)) |

Both stores attach to the external `data-net` network; the app
backends reach them by alias. `data-net` carries data-plane traffic
only — inference traffic runs on a separate `inference-net`.

## Why a separate compose project

The chorus and docint application compose files declare **no**
application-state volumes. The worst case from `docker compose down -v`
in those projects is a service restart — they cannot wipe graph or
vector data because that data lives in volumes owned by *this* project.

Those volumes are declared `external`, so even `docker compose down -v`
*here* leaves them intact — only `make nuke`, which deletes them by name
behind an interactive confirmation, can wipe the data set. Backups
(`backup/`) are the recovery path.

## Quick start

```bash
cp .env.example .env
$EDITOR .env                  # set NEO4J_PASSWORD at minimum

make network                  # create the external data-net (idempotent)
make volumes                  # create the external data volumes (idempotent)
make up                       # start with the CPU Qdrant profile (default), plus admin-audit
make up PROFILE=cuda          # GPU profile
make up-dev                   # publish ports on the host (Neo4j Browser, Qdrant UI)
make up-admin                 # production + loopback-only dashboard ports (see Admin access)
```

`make network` creates the external `data-net` if it does not exist; the
app backends (chorus, docint) join it when they come up. `make volumes`
pre-creates the named data volumes — they are declared `external`, so
compose will not create them itself. Both targets are idempotent and run
automatically as prerequisites of `make up`, so a fresh host can also just
run `make up`.

## Admin access (production)

Production shape publishes no ports, and the edge-plane gateway deliberately
does not route Neo4j Browser or the Qdrant dashboard (see
[`../edge-plane/README.md`](../edge-plane/README.md), "What is deliberately
NOT routed") — the SSH tunnel is their access model. `make up-admin` gives
that tunnel a target: it layers `docker/compose.admin.yaml`, which binds
7474/7687 (Neo4j http + bolt) and 6333 (Qdrant http) to `127.0.0.1` only.
Nothing becomes reachable from the network.

```bash
make up-admin                 # on the host
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 -L 6333:localhost:6333 <host>   # from your machine
# Neo4j Browser:    http://localhost:7474   (connects to bolt://localhost:7687)
# Qdrant dashboard: http://localhost:6333/dashboard
make up                       # drop the loopback ports again (brief container restart)
```

Neo4j Browser always asks for the `NEO4J_USER` / `NEO4J_PASSWORD` login. The
Qdrant dashboard has no login unless `QDRANT_API_KEY` is set in
`.env`: uncomment it there (see `.env.example` — an *empty* value is not "no
auth", it makes Qdrant 401 everything) and the dashboard prompts for the key,
while every API call needs an `api-key` header — docint's, the backup
script's, and yours over the tunnel. Without it, anyone who can reach the
loopback ports, and every service on `data-net`, has unauthenticated read and
write access to the vector store.

**Not recorded.** What is done over these ports bypasses the audit trail:
neither store logs it, and admin-audit only sees the make targets. For access
that is recorded, use `make db-shell` / `make qdrant-shell` ([Audit
trail](#audit-trail)).

## Operating

```bash
make ps                       # service state
make health                   # health + uptime; fails while admin-audit cannot record
make logs S=neo4j             # tail logs for one service
make db-shell REASON="..."    # cypher-shell on Neo4j — recorded, needs a purpose
make qdrant-shell REASON="..." # shell with Qdrant's HTTP API — recorded, needs a purpose
make down                     # stop, keep volumes
make bundle                   # airgap tarball from the latest release tag
make nuke                     # interactive: DESTROY all volumes
```

`make bundle` bundles the latest annotated release tag; set `DATA_PLANE_VERSION_OVERRIDE=<version>` to bundle the current working tree instead (bespoke Makefile — no `bundle-dev` target).

## Container hardening (deploy ADR 0001)

All services run with `no-new-privileges` and `cap_drop: ALL`. Neo4j re-adds
the five capabilities its entrypoint needs to chown/chmod its directories and
drop to the internal `neo4j` user, plus `KILL`, without which its init cannot
pass `docker stop`'s SIGTERM on to the server; and it stops with a 2-minute
grace period, as its shutdown outlasts Docker's default 10 s — verified
empirically, see the compose comment; Qdrant needs none. `admin-audit` also runs as `nobody` on a
read-only root filesystem, with no network and no data volumes (only
read-only binds of its code and the `VERSION` file).

For the two databases, `user:` and `read_only:` are **deliberately deferred**:
both manage their own on-disk state on external volumes with image-managed
ownership, and this repo's CI does not run them, so a UID change would land
untested. The residual root-daemon exposure is compensated host-side via
`userns-remap` — runbook in
[`../deploy/docs/runbooks/userns-remap.md`](../deploy/docs/runbooks/userns-remap.md),
rationale in
[`../deploy/docs/decisions/0001-container-engine-docker.md`](../deploy/docs/decisions/0001-container-engine-docker.md).

## Backup / restore

`make backup` writes a Neo4j offline dump and triggers Qdrant snapshots.
The Neo4j dump requires a brief stop of the service; Qdrant snapshots
are online. `make restore-neo4j DUMP=<file>` and
`make restore-qdrant COLLECTION=<name> SNAPSHOT=<file>` write one back (each
asks for confirmation first). All of them are recorded in the audit trail; a
backup needs a stated purpose by default (`make backup REASON="nightly
backup"`). Full runbook with retention, off-host copy, and verify-restore
steps in [`backup/README.md`](backup/README.md).

## Audit trail

What data-plane contributes to the federation's data-protection audit log
(§76 BDSG as the reference standard). The apps record what their users do
with the data in Neo4j and Qdrant; access that bypasses them is recorded
here, because neither store keeps a record of its own — Neo4j Community has
no query log, Qdrant no access log. The events are hash-chained JSON lines
(`audit/audit_event.py`, vendored from `nos-tromo/.github`). The store, its
retention, the chain verifier and who may read the trail are obs-plane's —
see [obs-plane's `docs/audit.md`](../obs-plane/docs/audit.md).

### The admin-audit service

`admin-audit` runs `audit/admin_audit.py` (stdlib Python, mounted read-only)
on the pinned python image. `make up` starts it with the stores, and
`make health` checks that it can record. Its own chain records its
configuration (`admin` / `config.audit`, with `require_purpose` and
`shells_require_purpose`) and beats every minute. Each record below is a chain of its own (`service: data-plane`),
written by a short `docker compose exec admin-audit python /app/admin_audit.py
…` run into that container's log (`AUDIT_OUTPUT=container`). That log is the
source obs-plane's verifier binds data-plane's chains to: the emitter
`data-plane/admin-audit`. The compose service is therefore named exactly
`admin-audit`.

The make targets run their operation through `scripts/audit-admin.sh`:

1. It asks admin-audit to admit the operation. It refuses with exit 2 when a
   purpose is required and none is stated (the refusal is recorded as
   `denied`) or when the stated one is malformed (refused unrecorded), and
   with exit 1 while admin-audit cannot record (stopped, misconfigured).
   Nothing runs then.
2. It runs the operation.
3. It records the outcome: `ok` when the operation exited 0, otherwise
   `error` (it may have run in part) — also when it was interrupted.

When the operation ran but its record could not be written, the target fails
loudly: record it by other means.

### What is recorded

| Target | `action` / `operation` | Written | `target` | Purpose |
|---|---|---|---|---|
| `make db-shell` | `admin` / `db.shell.open`, then `db.shell.close` | open before the session, close after it (its `parent_event_id` is the open) | `store`: `neo4j` | always required |
| `make qdrant-shell` | `admin` / `db.shell.open`, then `db.shell.close` | as above | `store`: `qdrant` | always required |
| `make backup-neo4j`, `make backup` | `disclose` / `backup.neo4j`, `recipient: file:backup` | after the dump | `database`: `neo4j` | when `AUDIT_REQUIRE_PURPOSE` lists `disclose` (the default) |
| `make backup-qdrant`, `make backup` | `disclose` / `backup.qdrant`, `recipient: file:backup` | after the snapshots | `collection`: each one snapshotted; beyond 200, `backup.qdrant.ids` events continue the list | as above |
| `make restore-neo4j DUMP=…` | `modify` / `restore.neo4j` | after the load | `database`: `neo4j` | when it lists `modify` (not by default) |
| `make restore-qdrant COLLECTION=… SNAPSHOT=…` | `modify` / `restore.qdrant` | after the recovery | `collection`: the one recovered | as above |

Every event names the operator, `actor: {user: <sudo user, else the login
user>, via: "cli"}`, and carries `purpose: {case_ref, reason}` when one was
stated. The `criteria`:

| Event | `criteria` |
|---|---|
| `db.shell.open` | `store` |
| `db.shell.close` | `store`, `exit_status`, `duration_s` |
| `backup.neo4j` | `file` (the dump's name), `size_bytes`, `exit_status`, `duration_s` |
| `backup.qdrant` | `snapshots`, `size_bytes` (their total, as Qdrant reports it), `failed` (collections not snapshotted), `exit_status`, `duration_s` |
| `restore.neo4j` | `file`, `size_bytes`, `exit_status`, `duration_s` |
| `restore.qdrant` | `collection`, `snapshot`, `source` (`host`: a copy in the backup directory; `volume`: from Qdrant's snapshot volume), `exit_status`, `duration_s` |

A refusal is the operation's event with `outcome: denied`, its criteria and
no target. Never recorded: what a session read or changed, file contents,
paths (file names only — a path can name a user), the Neo4j password or the
Qdrant key.

### Stating the purpose

```bash
make db-shell CASE_REF=case-0001 REASON="verify a reported duplicate"
make backup REASON="nightly backup"     # a scheduled backup states one too
```

Either field is enough. The purpose is personal data in its own right:
prefer a case reference to a name. make hands command-line variables to the
recipe's environment, quotes included (a `$` is expanded by make).

`AUDIT_REQUIRE_PURPOSE` (in `.env`, read by admin-audit) lists the actions
whose records need a stated purpose. Unset, it is `query,disclose`: backups
need one, restores do not. `AUDIT_REQUIRE_PURPOSE=` (empty) requires none;
`query,disclose,modify` requires one for restores too. The shells need one
whatever it says. An unknown action stops admin-audit, and every target
refuses until it is fixed: a typo never switches the requirement off.

### What it does not cover

The records cover the make targets. Whoever can use the Docker daemon can
reach the stores without them: `docker exec`, and the loopback ports of
`make up-admin` (the Neo4j Browser and the Qdrant dashboard over the SSH
tunnel, [Admin access](#admin-access-production)). That access is
root-equivalent and is the host's to audit. Inside a shell session, nothing
the session does is recorded; its opening and close are.

### Checking it

```bash
make health                     # ends with "admin-audit: can record"
make logs S=admin-audit         # the events (stdout) and operational messages (stderr)
docker compose --env-file .env -f docker/compose.yaml logs --no-log-prefix admin-audit \
  | docker compose --env-file .env -f docker/compose.yaml exec -T admin-audit python /app/admin_audit.py verify
python3 -m unittest discover -s audit/tests -t audit   # the tests, synthetic data only
```

## Layout

```
data-plane/
  docker/
    compose.yaml          production-shape compose (no host ports)
    compose.override.yaml dev overlay — publishes ports on the host
    compose.admin.yaml    admin overlay — loopback-only ports for SSH-tunnelled dashboards
  audit/
    admin_audit.py        admin-audit: the audit records (stdlib Python, mounted read-only)
    audit_event.py        audit-event library, vendored from nos-tromo/.github
    audit_runtime.py      audit runtime glue, vendored from nos-tromo/.github
    tests/                unittest suite (synthetic data)
  scripts/
    audit-admin.sh        runs an operation and records it through admin-audit
    neo4j_admin.sh        cypher-shell session, offline dump and load
    qdrant_admin.sh       API shell, snapshot of every collection, recovery of one
    qdrant_api.sh         Qdrant's HTTP API over /dev/tcp, run inside the container
    qdrant_snapshot.sh    snapshot-every-collection script, run inside the container
    bundle_images.sh      airgap bundler (sources the vendored bundle-lib.sh)
    bundle-lib.sh         shared bundle library, vendored from nos-tromo/.github
  .env.example            copy to .env
  Makefile                operator commands
  VERSION                 release version read by the release-tag workflow
  backup/                 runbooks + snapshot drop location
```

## Pointers

- chorus architecture contract: `../chorus/docs/architecture.md`
  ("Data-plane integration contract")
- chorus invariant — vectors live in Neo4j, not Qdrant: `../chorus/docs/decisions/0003-vectors-in-neo4j.md`
- docint Qdrant usage: `../docint/docker/compose.yaml`
- Ordered federation bring-up: [`../deploy/README.md`](../deploy/README.md)
- Questions and bugs: <https://github.com/nos-tromo/data-plane/issues>
