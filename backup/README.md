# Backup + restore runbook

Backups for the two stateful services this project owns.

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
make backup-neo4j
# wraps:
#   docker compose stop neo4j
#   docker compose run --rm --entrypoint /bin/bash neo4j \
#     -c "neo4j-admin database dump neo4j --to-path=/backup"
#   docker compose start neo4j
# writes ./backup/snapshots/neo4j-<ISO8601-utc>.dump
```

Downtime: typically a few seconds for an empty graph, growing roughly
linearly with dataset size. Schedule it during the maintenance window
chorus already publishes for retention sweeps.

### Restore

```bash
# 0. Stop the service.
docker compose -f compose.yaml stop neo4j

# 1. Mount the dump into a one-shot container and load it. This
#    OVERWRITES the existing neo4j database in the data volume.
docker compose -f compose.yaml run --rm \
  -v "$PWD/backup/snapshots:/backup" \
  --entrypoint /bin/bash neo4j -c \
  "neo4j-admin database load neo4j --from-path=/backup --overwrite-destination=true"

# 2. Start the service. Chorus migrations will re-run idempotently on
#    the next chorus deploy and bring the schema forward if needed.
docker compose -f compose.yaml start neo4j
```

Validate after restore: open the Neo4j Browser (dev: http://localhost:7474),
run `MATCH (n) RETURN count(n)`, and spot-check entity counts against the
record kept at backup time.

### Cadence

Default suggestion: **nightly** dumps with **14-day** local retention,
**90-day** retention on off-host storage. Tighten if the data justifies
it; the offline dump window is the binding constraint.

## Qdrant

Qdrant supports online snapshots via its HTTP API. No service downtime.

### Backup

```bash
make backup-qdrant
# wraps: POST http://qdrant:6333/collections/<name>/snapshots
# Snapshots land inside the qdrant-snapshots volume, NOT on the host.
```

To get a snapshot off-host:

```bash
# Replace <collection> and <snapshot>.
docker compose -f compose.yaml cp \
  qdrant-cpu:/qdrant/snapshots/<collection>/<snapshot> \
  ./backup/snapshots/
```

If `wget` is absent from the running Qdrant image (some slim builds), use
the API from the host instead while `make up-dev` is active:

```bash
curl -X POST http://localhost:6333/collections/<collection>/snapshots
```

### Restore

```bash
# Push the snapshot back into the container, then recover it via API.
docker compose -f compose.yaml cp \
  ./backup/snapshots/<snapshot> \
  qdrant-cpu:/qdrant/snapshots/<collection>/<snapshot>

curl -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"location":"file:///qdrant/snapshots/<collection>/<snapshot>"}' \
  http://localhost:6333/collections/<collection>/snapshots/recover
```

Snapshot recovery does not require stopping Qdrant.

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

Document the result (passed / restore-time / data-completeness) in the
operations log.
