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
$EDITOR .env                  # set NEO4J_PASSWORD and QDRANT_API_KEY (openssl rand -hex 32)

make network                  # create the external data-net (idempotent)
make volumes                  # create the external data volumes (idempotent)
make up                       # start with the CPU Qdrant profile (default), plus admin-audit
make up PROFILE=cuda          # GPU profile
make up-dev                   # publish ports on the host (Neo4j Browser, Qdrant UI)
make up-admin REASON="..."    # production + loopback-only dashboard ports, recorded (see Admin access)
```

`make network` creates the external `data-net` if it does not exist; the
app backends (chorus, docint) join it when they come up. `make volumes`
pre-creates the named data volumes — they are declared `external`, so
compose will not create them itself. Both targets are idempotent and run
automatically as prerequisites of `make up`, so a fresh host can also just
run `make up`.

`NEO4J_PASSWORD` and `QDRANT_API_KEY` are both required: compose refuses
every command without them. docint must be given the same `QDRANT_API_KEY`
in its own `.env`.

**Upgrading an existing deployment to 0.5.0:** set `QDRANT_API_KEY` in
data-plane's `.env` and the same value in docint's `.env` (docint >= v2.5.0)
before running `make up` here, then `make up` in docint. Until docint has
the key, Qdrant refuses its requests (401).

**Upgrading to 0.6.0:** backups are encrypted with age now, and refuse to run
until the host has age and a recipients file of public keys
([`backup/README.md`](backup/README.md), Encryption). Restores take
`IDENTITY=` and encrypted backups only: encrypt or delete the unencrypted
backups earlier versions left (same file, Plaintext from earlier versions).

## Admin access (production)

Production shape publishes no ports, and the edge-plane gateway deliberately
does not route Neo4j Browser or the Qdrant dashboard (see
[`../edge-plane/README.md`](../edge-plane/README.md), "What is deliberately
NOT routed") — the SSH tunnel is their access model. `make up-admin` gives
that tunnel a target, on demand: it layers `docker/compose.admin.yaml`, which
binds 7474/7687 (Neo4j http + bolt) and 6333 (Qdrant http) to `127.0.0.1`
only. Nothing becomes reachable from the network. Production runs plain
`make up` (deploy's bring-up too); the ports are open only between a
`make up-admin` and the next `make up` or `make down`.

```bash
make up-admin CASE_REF=case-0001 REASON="inspect the graph schema"   # on the host
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 -L 6333:localhost:6333 <host>   # from your machine
# Neo4j Browser:    http://localhost:7474   (connects to bolt://localhost:7687)
# Qdrant dashboard: http://localhost:6333/dashboard
make up                       # drop the loopback ports again (brief container restart)
```

Opening the ports always needs `CASE_REF=` or `REASON=`, and is recorded
before anything is published (`admin` / `db.ports.open`; refused while that
cannot be written, or while a window is already open). Closing them — `make
up`, or `make down` — records `admin` / `db.ports.close`, linked to the
opening, with how long the ports were open ([Audit trail](#audit-trail)). Between
the two, the open window is kept in `.admin-ports`, a host-local file only
the operator can read. `make health` says whether the ports are open, and
since when. `make stop` does not close them (a start brings them back), and
`make nuke` does not record the close; the next `make up` does.

Neo4j Browser always asks for the `NEO4J_USER` / `NEO4J_PASSWORD` login, and
the Qdrant dashboard for the `QDRANT_API_KEY` from `.env`. Every Qdrant API
call needs that key as the `api-key` header — docint's, and yours over the
tunnel; `make backup-qdrant`, `make qdrant-shell` and `make restore-qdrant`
send the server's own key themselves.

**What is done over these ports is not recorded**, only that they were open,
by whom, why and for how long: neither store logs it, and admin-audit only
sees the make targets. For access that is recorded session by session, use
`make db-shell` / `make qdrant-shell` ([Audit trail](#audit-trail)).

## Operating

```bash
make ps                       # service state
make health                   # health + uptime, the admin ports; fails while admin-audit cannot record
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

`make backup` writes a Neo4j offline dump and Qdrant snapshots, each
encrypted with [age](https://age-encryption.org) on its way out of the store,
to the public keys in `backup/recipients.txt` (`BACKUP_RECIPIENTS`); the
identity that decrypts stays offline, and no unencrypted dump or snapshot
rests on the host. age comes from the host's package manager. The Neo4j dump
requires a brief stop of the service; Qdrant snapshots are online.
`make restore-neo4j DUMP=<file> IDENTITY=<file>` and
`make restore-qdrant COLLECTION=<name> SNAPSHOT=<file> IDENTITY=<file>`
decrypt one back with the identity given for that run (each asks for
confirmation first). All of them are recorded in the audit trail; a backup
needs a stated purpose by default (`make backup REASON="scheduled backup"`).
Full runbook with installing age, the key pair, retention, off-host copy,
scheduling and verify-restore steps in [`backup/README.md`](backup/README.md).

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

The make targets run their operation through `scripts/audit-admin.sh` (the
admin ports through `scripts/admin-ports.sh`, which records their opening
and close the same way, [Admin access](#admin-access-production)):

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
| `make up-admin`, then `make up` or `make down` | `admin` / `db.ports.open`, then `db.ports.close` | open before the ports are published, close when `make up` has removed them or before `make down` does (its `parent_event_id` is the open) | `store`: `neo4j`, `qdrant` | always required (for the opening) |
| `make backup-neo4j`, `make backup` | `disclose` / `backup.neo4j`, `recipient: file:backup` | after the dump | `database`: `neo4j` | when `AUDIT_REQUIRE_PURPOSE` lists `disclose` (the default) |
| `make backup-qdrant`, `make backup` | `disclose` / `backup.qdrant`, `recipient: file:backup` | after the snapshots | `collection`: each one snapshotted; beyond 200, `backup.qdrant.ids` events continue the list | as above |
| `make restore-neo4j DUMP=… IDENTITY=…` | `modify` / `restore.neo4j` | after the load | `database`: `neo4j` | when it lists `modify` (not by default) |
| `make restore-qdrant COLLECTION=… SNAPSHOT=… IDENTITY=…` | `modify` / `restore.qdrant` | after the recovery | `collection`: the one recovered | as above |

Every event names the operator, `actor: {user: <sudo user, else the login
user>, via: "cli"}`, and carries `purpose: {case_ref, reason}` when one was
stated. The `criteria`:

| Event | `criteria` |
|---|---|
| `db.shell.open` | `store` |
| `db.shell.close` | `store`, `exit_status`, `duration_s` |
| `db.ports.open` | `ports` (the loopback bindings) |
| `db.ports.close` | `ports`, `by` (`up`, `down`, or `up-admin` when the opening failed and published nothing), `exit_status`, `duration_s` (how long they were open) |
| `backup.neo4j` | `file` (the encrypted dump's name), `size_bytes`, `encryption` (`age`), `recipients` (how many public keys can read it), `recipients_sha256` (the fingerprint of that set of keys, [`backup/README.md`](backup/README.md)), `exit_status`, `duration_s` |
| `backup.qdrant` | `snapshots`, `size_bytes` (the encrypted files' total), `failed` (collections not backed up), `encryption`, `recipients`, `recipients_sha256`, `exit_status`, `duration_s` |
| `restore.neo4j` | `file`, `size_bytes`, `encryption`, `exit_status`, `duration_s` |
| `restore.qdrant` | `collection`, `snapshot`, `encryption`, `exit_status`, `duration_s` |

A refusal is the operation's event with `outcome: denied`, its criteria and
no target. A backup or restore recorded as `ok` always states its encryption:
admin-audit refuses such a record without it. Never recorded: what a session
read or changed, file contents, paths (file names only — a path can name a
user), the Neo4j password, the Qdrant key, the public keys, or the identity a
restore used and where it is.

### Stating the purpose

```bash
make db-shell CASE_REF=case-0001 REASON="verify a reported duplicate"
make backup REASON="scheduled backup"   # a scheduled backup states one too
```

Either field is enough. The purpose is personal data in its own right:
prefer a case reference to a name. make hands command-line variables to the
recipe's environment, quotes included (a `$` is expanded by make).

`AUDIT_REQUIRE_PURPOSE` (in `.env`, read by admin-audit) lists the actions
whose records need a stated purpose. Unset, it is `query,disclose`: backups
need one, restores do not. `AUDIT_REQUIRE_PURPOSE=` (empty) requires none;
`query,disclose,modify` requires one for restores too. The shells need one
whatever it says, and so does opening the admin ports. An unknown action
stops admin-audit, and every target refuses until it is fixed: a typo never
switches the requirement off.

### What it does not cover

The records cover the make targets. Whoever can use the Docker daemon can
reach the stores without them (`docker exec`, or publishing ports by hand,
which `make health` then reports as open without an open record). That
access is root-equivalent and is the host's to audit. Inside a shell session,
nothing the session does is recorded, nor what is done over the admin ports
while they are open; their opening and close are. A backup is recorded when
it is written, not where its copies go: whoever holds one of the identities
can decrypt a copy anywhere, unrecorded, which is why the identities stay
offline.

### Checking it

```bash
make health                     # the admin ports' state, then "admin-audit: can record"
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
    admin-ports.sh        opens and closes the admin overlay's ports, recorded
    neo4j_admin.sh        cypher-shell session, offline dump and load
    qdrant_admin.sh       API shell, snapshot of every collection, recovery of one
    qdrant_api.sh         Qdrant's HTTP API over /dev/tcp, run inside the container
    backup_age.sh         the age encryption of backups: recipients and identity checks
    bundle_images.sh      airgap bundler (sources the vendored bundle-lib.sh)
    bundle-lib.sh         shared bundle library, vendored from nos-tromo/.github
  .env.example            copy to .env
  Makefile                operator commands
  VERSION                 release version read by the release-tag workflow
  backup/                 runbook; the recipients file and encrypted backups (gitignored)
```

## Pointers

- chorus architecture contract: `../chorus/docs/architecture.md`
  ("Data-plane integration contract")
- chorus invariant — vectors live in Neo4j, not Qdrant: `../chorus/docs/decisions/0003-vectors-in-neo4j.md`
- docint Qdrant usage: `../docint/docker/compose.yaml`
- Ordered federation bring-up: [`../deploy/README.md`](../deploy/README.md)
- Questions and bugs: <https://github.com/nos-tromo/data-plane/issues>
