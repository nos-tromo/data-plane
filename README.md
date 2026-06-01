# data-plane

Stateful services for the nos-tromo apps. This project owns the named
Docker volumes for graph and vector data; the apps (chorus, docint) stay
stateless and disposable.

## What lives here

| Service  | Used by  | Network alias on `data-net` | Why                    |
|----------|----------|-----------------------------|------------------------|
| `neo4j`  | chorus   | `neo4j`                     | Graph + native vectors |
| `qdrant` | docint   | `qdrant`                    | Document vector store  |

Both services attach to the external `data-net` network; the app
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
make up                       # start with the CPU Qdrant profile (default)
make up PROFILE=cuda          # GPU profile
make up-dev                   # publish ports on the host (Neo4j Browser, Qdrant UI)
```

`make network` creates the external `data-net` if it does not exist; the
app backends (chorus, docint) join it when they come up. `make volumes`
pre-creates the named data volumes — they are declared `external`, so
compose will not create them itself. Both targets are idempotent and run
automatically as prerequisites of `make up`, so a fresh host can also just
run `make up`.

## Operating

```bash
make ps                       # service state
make health                   # health + uptime
make logs S=neo4j             # tail logs for one service
make down                     # stop, keep volumes
make bundle                   # save images as a versioned airgap tarball
make nuke                     # interactive: DESTROY all volumes
```

## Backup / restore

`make backup` writes a Neo4j offline dump and triggers Qdrant snapshots.
The Neo4j dump requires a brief stop of the service; Qdrant snapshots
are online. Full runbook with retention, off-host copy, and verify-restore
steps in [`backup/README.md`](backup/README.md).

## Layout

```
data-plane/
  docker/
    compose.yaml          production-shape compose (no host ports)
    compose.override.yaml dev overlay — publishes ports on the host
  .env.example            copy to .env
  Makefile                operator commands
  backup/                 runbooks + snapshot drop location
```

## Pointers

- chorus architecture contract: `../chorus/docs/architecture.md`
  ("Data-plane integration contract")
- chorus invariant — vectors live in Neo4j, not Qdrant: `../chorus/docs/decisions/0003-vectors-in-neo4j.md`
- docint Qdrant usage: `../docint/docker-compose.yml`
