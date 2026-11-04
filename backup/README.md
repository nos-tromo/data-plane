# Backup + restore runbook

Backups for the two stateful services this project owns.

Every backup and restore target is recorded in the data-protection audit
trail by `admin-audit` (README § Audit trail): a backup as `disclose` /
`backup.<store>` to `file:backup`, a restore as `modify` / `restore.<store>`.
They refuse to run while admin-audit cannot record them (start it with
`make up`). Under the default `AUDIT_REQUIRE_PURPOSE` a backup also needs a
stated purpose; give a scheduled one a fixed reason, e.g. from cron:

```bash
make -C <data-plane checkout> backup REASON="nightly backup"
```

## Where snapshots land

```
backup/
  README.md            this file
  snapshots/           local drop directory (gitignored; created on demand)
```

For real operations, copy `snapshots/` off-host as soon as the backup
finishes. This directory is *staging*, not durable storage.

## Neo4j (community edition)

Community edition has **no online backup**. The supported path is an
offline `neo4j-admin database dump` while the service is stopped.

### Backup

```bash
make backup-neo4j REASON="nightly backup"
# runs scripts/neo4j_admin.sh dump through scripts/audit-admin.sh:
#   docker compose stop neo4j
#   docker compose run --rm -v "$PWD/backup/snapshots:/backup" \
#     --entrypoint /bin/bash neo4j -c \
#     "neo4j-admin database dump neo4j --to-path=/backup \
#        --overwrite-destination=true && \
#      mv /backup/neo4j.dump /backup/neo4j-<ISO8601-utc>.dump"
#   docker compose start neo4j        # also when the dump failed
# writes ./backup/snapshots/neo4j-<ISO8601-utc>.dump
# records disclose / backup.neo4j with the file's name and size
```

Downtime: around 20 seconds for a small graph (a clean stop alone takes
about 10 s), growing roughly linearly with dataset size. Schedule it during the maintenance window
chorus already publishes for retention sweeps.

The dump needs Neo4j to have shut down cleanly. That is why its container
keeps `CAP_KILL`: without it tini cannot pass `docker stop`'s SIGTERM on to
the server, the server is killed, and `neo4j-admin` refuses the store
("Active logical log detected"). It also stops with a 2-minute grace period:
the shutdown takes about 10 s even for a small graph, longer than Docker's
default.

### Restore

```bash
make restore-neo4j DUMP=neo4j-<ISO8601-utc>.dump   # a dump in ./backup/snapshots; asks first
# runs scripts/neo4j_admin.sh load through scripts/audit-admin.sh:
#   docker compose stop neo4j
#   docker compose run --rm \
#     -v "$PWD/backup/snapshots/<dump>:/backup/neo4j.dump:ro" \
#     --entrypoint /bin/bash neo4j -c \
#     "neo4j-admin database load neo4j --from-path=/backup --overwrite-destination=true"
#   docker compose start neo4j        # also when the load failed
# records modify / restore.neo4j with the dump's name and size
```

This **overwrites** the neo4j database in the data volume. `neo4j-admin`
loads `<database>.dump` from `--from-path`, so the chosen dump is mounted as
`neo4j.dump`. Chorus migrations re-run idempotently on the next chorus
deploy and bring the schema forward if needed.

Validate after restore: run `MATCH (n) RETURN count(n);` in
`make db-shell REASON="validate restore"` (recorded), or in the Neo4j Browser
(dev: `make up-dev`; production: `make up-admin` + SSH tunnel, see README
§ Admin access), and spot-check entity counts against the record kept at
backup time.

### Cadence

Default suggestion: **nightly** dumps with **14-day** local retention,
**90-day** retention on off-host storage. Tighten if the data justifies
it; the offline dump window is the binding constraint.

## Qdrant

Qdrant supports online snapshots via its HTTP API. No service downtime.

### Backup

```bash
make backup-qdrant REASON="nightly backup"
# wraps: POST http://localhost:6333/collections/<name>/snapshots
#        (localhost — the script runs inside the Qdrant container)
# Snapshots land inside the qdrant-snapshots volume, NOT on the host.
# records disclose / backup.qdrant with the collections and their total size
```

The target pipes `scripts/qdrant_snapshot.sh` into the container's `bash`,
which talks to the API over bash's built-in `/dev/tcp` — the Qdrant image
ships no `curl`/`wget`/`python3`, and this avoids needing any of them.

To get a snapshot off-host:

```bash
# Replace <collection> and <snapshot>.
docker compose --env-file .env -f docker/compose.yaml cp \
  qdrant-cpu:/qdrant/snapshots/<collection>/<snapshot> \
  ./backup/snapshots/
```

If the running Qdrant image has no `bash` at all (the target then fails
loudly), use the API from the host instead while `make up-dev` is active
(not recorded: record it by other means):

```bash
curl -X POST http://localhost:6333/collections/<collection>/snapshots
```

With `QDRANT_API_KEY` set in `.env`, `make backup-qdrant` keeps working
unchanged — the script runs inside the container and reads the server's own
key from its environment, where compose passes it as
`QDRANT__SERVICE__API_KEY`. Every host-side call above needs the header added
instead, `-H 'api-key: <value>'`, including the `curl` fallback and anything
you run over the admin SSH tunnel.

### Restore

```bash
make restore-qdrant COLLECTION=<collection> SNAPSHOT=<snapshot>   # asks first
# runs scripts/qdrant_admin.sh recover through scripts/audit-admin.sh:
#   ./backup/snapshots/<snapshot> present -> streamed into the container as
#     /qdrant/snapshots/<collection>/<snapshot> (a copy brought back from off-host)
#   otherwise -> that path in the qdrant-snapshots volume, where backup-qdrant left it
#   PUT /collections/<collection>/snapshots/recover
#       {"location":"file:///qdrant/snapshots/<collection>/<snapshot>"}
#   (from inside the container, with the container's own api-key)
# records modify / restore.qdrant with the collection, snapshot and source
```

This **replaces** the collection. Snapshot recovery does not require
stopping Qdrant, and runs inside the container, so no port needs publishing.

A host copy is streamed in rather than copied with `docker compose cp`: cp
keeps the host file's owner and mode (a snapshot copied out is `0600` and
belongs to whoever copied it), and Qdrant runs as root without
capabilities, so it cannot open a file it does not own — the recovery fails
with "Permission denied".

### Cadence

Default suggestion: **hourly** snapshots (cheap, online) with **24-hour**
in-volume retention, **30-day** retention off-host. Qdrant prunes nothing
on its own — schedule snapshot cleanup or the volume will grow forever.

## Verify-restore drill

A backup you have never restored is not a backup. Quarterly:

1. Spin up a scratch data-plane project (`docker compose -p data-plane-drill ...`).
2. Restore the most recent Neo4j dump and most recent Qdrant snapshot.
3. Run the chorus and docint health endpoints against the drill stack.
4. Tear down the drill project.

**The volumes are external and named.** A scratch project started from
`docker/compose.yaml` attaches to the production volumes, whatever its
project name, and a restore there overwrites production data. Drill on a
separate host, or with a compose file whose volumes are the drill's own.
The drill's `admin-audit` records under the drill's project name; obs-plane's
verifier reports such chains (`foreign_source`, `unlisted_source`), so drill
where obs-plane does not collect, or expect those reports.

Document the result (passed / restore-time / data-completeness) in the
operations log.
