# Backup + restore runbook

Backups for the two stateful services this project owns.

Every backup is encrypted with [age](https://age-encryption.org) on its way
out of the store, to the public keys in a recipients file. The identity that
decrypts stays offline; a restore takes it for that run only. No unencrypted
dump or snapshot rests on the host: the Neo4j dump streams from `neo4j-admin`
through age, each Qdrant snapshot is streamed out of the container through age
and deleted from Qdrant's volume at once, and a restore decrypts straight into
the store, never to a file.

Every backup and restore target is recorded in the data-protection audit
trail by `admin-audit` (README § Audit trail): a backup as `disclose` /
`backup.<store>` to `file:backup`, with its encryption and how many public
keys can read it, a restore as `modify` / `restore.<store>`. They refuse to
run while admin-audit cannot record them (start it with `make up`). Under the
default `AUDIT_REQUIRE_PURPOSE` a backup also needs a stated purpose; a
scheduled one passes a fixed reason ([Scheduling](#scheduling)).

## Encryption

### Installing age

age runs on the host, from the distribution's package: a single static binary
without runtime dependencies, which fetches nothing.

| Host | Install |
|---|---|
| Debian 12 or later, Ubuntu 22.04 or later | `sudo apt install age` |
| RHEL, Rocky Linux, AlmaLinux 8 or 9 | from EPEL: `sudo dnf install epel-release` (on RHEL itself, enable EPEL as Red Hat documents it), then `sudo dnf install age` |
| Fedora | `sudo dnf install age` |

On an airgapped host, download the package on a connected machine running the
same release (`apt-get download age`, which checks it against the signed
package index, or `dnf download age`, then `rpm -K age-*.rpm` with the EPEL
key imported), carry it over with the bundles, and install it from the file
(`sudo apt install ./age_*.deb`, `sudo dnf install ./age-*.rpm`). Install it
wherever restores are run, too.

Why a host package and not an image that `make bundle` would carry: age's
author publishes no container image, and the ones that exist are third-party
builds with nothing to verify them by, for a tool that sees the whole
database in plaintext. A distribution package is signed by the distribution,
the trust the host already gives its Docker engine. And a restore needs age
where the operator is: a passphrase-protected identity asks for its
passphrase on the terminal, a hardware-backed one needs its age plugin, and
neither works from inside a container.

### The key pair

Create it **offline**, on a machine other than the data-plane host:

```bash
age-keygen -o data-plane-backup.key   # the identity: it stays offline
age-keygen -y data-plane-backup.key   # its public key, age1...
```

Keep the identity off the host, e.g. on removable media in a safe. It may be
passphrase-protected (`age -p -o data-plane-backup.key.age
data-plane-backup.key`, then destroy the unprotected file): age asks for the
passphrase whenever the identity is used. With several keyholders, each makes
a key pair of their own; any one identity decrypts every backup.

### The recipients file

On the data-plane host, put each keyholder's public key on a line of its own
into `backup/recipients.txt` (gitignored, as is all of `backup/` but this
runbook), or into the file `BACKUP_RECIPIENTS` names in `.env` or on the
command line:

```
# data-plane backups: keyholder one, keyholder two
age1...
age1...
```

Only public keys belong there. Every backup refuses, before anything else
happens and with how to create the key pair, when the file is missing, holds a
private key, can be changed by anyone (whoever can change it chooses who reads
the next backup), or has a line age does not take as a public key. A key from
`age-keygen` works with every age release; a post-quantum one (`age-keygen
-pq`) needs age 1.3 or later on the host, and a plugin key (`age1yubikey1…`)
its plugin there.

To rotate, add the new public key, and remove the old one once its holder
should read no further backups. Earlier backups stay readable with the
identities they were encrypted to only, so keep an identity for as long as
backups encrypted to it are kept. Each backup's record says which set of keys
can read it: `recipients_sha256`, the SHA-256 of the distinct public keys,
sorted, one per line. For a recipients file:

```bash
tr -d '\r' < backup/recipients.txt | grep -vE '^(#|$)' | LC_ALL=C sort -u | sha256sum
```

## Where backups land

```
backup/
  README.md            this file
  recipients.txt       the public keys backups are encrypted to (operator-provided, gitignored)
  snapshots/           the encrypted backups (gitignored; created on demand)
    neo4j-<ISO8601-utc>.dump.age
    qdrant-<collection>-<ISO8601-utc>.snapshot.age
```

The files belong to whoever ran the backup, mode `0600`. Copy `snapshots/`
off-host as soon as the backup finishes: this directory is *staging*, not
durable storage. The copies stay encrypted; keep the identity apart from them.
Nothing prunes `snapshots/`: remove what is older than the local retention
once it is copied ([Cadence](#cadence)).

## Neo4j (community edition)

Community edition has **no online backup**. The supported path is an
offline `neo4j-admin database dump` while the service is stopped.

### Backup

```bash
make backup-neo4j REASON="scheduled backup"
# checks the recipients file, then runs scripts/neo4j_admin.sh dump through scripts/audit-admin.sh:
#   docker compose stop neo4j
#   docker compose run --rm -T --entrypoint neo4j-admin neo4j \
#     database dump neo4j --to-stdout \
#     | age -R backup/recipients.txt -o backup/snapshots/neo4j-<ISO8601-utc>.dump.age.part
#   mv ….dump.age.part ….dump.age       # only when neo4j-admin and age both succeeded
#   docker compose start neo4j          # also when the dump failed or was interrupted
# records disclose / backup.neo4j with the file's name and size, encryption: age and the recipients
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
make restore-neo4j DUMP=neo4j-<ISO8601-utc>.dump.age IDENTITY=<identity file>   # a dump in ./backup/snapshots; asks first
# checks the identity and that the dump is age-encrypted, asks, then runs
# scripts/neo4j_admin.sh load through scripts/audit-admin.sh:
#   age -d -i <identity> <dump>          # the whole file, checked to hold a Neo4j dump, while Neo4j still runs
#   docker compose stop neo4j
#   age -d -i <identity> <dump> \
#     | docker compose run --rm -T --entrypoint neo4j-admin neo4j \
#         database load neo4j --from-stdin --overwrite-destination=true
#                                        # neo4j-admin starts only once the dump's first bytes arrived
#   docker compose start neo4j          # also when the load failed
# records modify / restore.neo4j with the dump's name and size, encryption: age
```

This **overwrites** the neo4j database in the data volume. `neo4j-admin`
replaces the database before it has read a byte of its input: an empty or
broken stream leaves Neo4j with an empty database. Hence the first pass,
which decrypts the whole file (age authenticates every part of it) and checks
that it holds a Neo4j dump: another identity, a damaged file, or a Qdrant
snapshot given as a dump never stops Neo4j. And the load starts only once the
second decryption has delivered the dump's first bytes, so one that fails at
once (a mistyped passphrase) never reaches it. A passphrase-protected
identity is therefore asked for twice. A restore that is interrupted while
`neo4j-admin` loads leaves a partial database behind; run it again.

The identity is used for that run only and is never stored or recorded; take
it off the host again afterwards. A restore takes only encrypted dumps ([Plaintext
from earlier versions](#plaintext-from-earlier-versions)). Chorus migrations
re-run idempotently on the next chorus deploy and bring the schema forward if
needed.

Validate after restore: run `MATCH (n) RETURN count(n);` in
`make db-shell REASON="validate restore"` (recorded), or in the Neo4j Browser
(dev: `make up-dev`; production: `make up-admin REASON="validate restore"`
+ SSH tunnel, recorded, see README § Admin access; `make up` closes the ports
again), and spot-check entity counts against the record kept at backup time.

### Cadence

Default suggestion: **nightly** dumps with **14-day** local retention,
**90-day** retention on off-host storage. Tighten if the data justifies
it; the offline dump window is the binding constraint.

## Qdrant

Qdrant supports online snapshots via its HTTP API. No service downtime.

### Backup

```bash
make backup-qdrant REASON="scheduled backup"
# checks the recipients file, then for each collection, from inside the Qdrant container
# (scripts/qdrant_admin.sh snapshot, through scripts/audit-admin.sh):
#   POST /collections/<collection>/snapshots                      # Qdrant writes it into its volume
#   docker compose exec -T qdrant-cpu cat /qdrant/snapshots/<collection>/<snapshot> \
#     | age -R backup/recipients.txt -o backup/snapshots/qdrant-<collection>-<ISO8601-utc>.snapshot.age.part
#   DELETE /collections/<collection>/snapshots/<snapshot>         # at once, also when the copy failed
#   mv ….snapshot.age.part ….snapshot.age                         # only when the copy succeeded
# records disclose / backup.qdrant with the collections, their total size, encryption: age and the recipients
```

Qdrant's API can write a snapshot only into its own `qdrant-snapshots`
volume, so each snapshot exists there, unencrypted and next to the live
collection it was made from, for as long as its copy takes, and is deleted
right after, also when the copy failed or was interrupted. A snapshot that
cannot be deleted fails the backup. Every backup reports unencrypted files
still in the volume, or in `backup/snapshots/`, without touching them ([Plaintext
from earlier versions](#plaintext-from-earlier-versions)). In the host file's
name, characters a file name should not carry become `_`; the record names
the collections as Qdrant does.

The API calls run inside the container over bash's built-in `/dev/tcp`: the
Qdrant image ships no `curl`/`wget`/`python3`, and this avoids needing any
of them. They need no key on the command line either: they read the server's
own key (`QDRANT_API_KEY` in `.env`, required) from the container's
environment, where compose passes it as `QDRANT__SERVICE__API_KEY`. Every
host-side call needs the header, `-H 'api-key: <value>'`, as in the `curl`
fallback below and anything you run over the admin SSH tunnel.

If the running Qdrant image has no `bash` at all (the target then fails
loudly), take the snapshot from the host while `make up-dev` is active, and
keep it encrypted the same way (not recorded: record it by other means):

```bash
key=<QDRANT_API_KEY>
curl -fsS -X POST -H "api-key: $key" http://localhost:6333/collections/<collection>/snapshots   # note the snapshot's name
curl -fsS -H "api-key: $key" http://localhost:6333/collections/<collection>/snapshots/<snapshot> \
  | age -R backup/recipients.txt -o backup/snapshots/<snapshot>.age
curl -fsS -X DELETE -H "api-key: $key" http://localhost:6333/collections/<collection>/snapshots/<snapshot>
```

### Restore

```bash
make restore-qdrant COLLECTION=<collection> SNAPSHOT=qdrant-<collection>-<ISO8601-utc>.snapshot.age IDENTITY=<identity file>   # asks first
# checks the identity and that the snapshot is age-encrypted, asks, then runs
# scripts/qdrant_admin.sh recover through scripts/audit-admin.sh:
#   age -d -i <identity> <snapshot> \
#     | docker compose exec -T qdrant-cpu sh -c 'cat > /qdrant/snapshots/<collection>/<snapshot>.part'   # private
#   mv <snapshot>.part <snapshot>       # only when age and the copy both succeeded
#   PUT /collections/<collection>/snapshots/recover
#       {"location":"file:///qdrant/snapshots/<collection>/<snapshot>"}
#   rm the decrypted snapshot from the volume                 # whatever the outcome
# records modify / restore.qdrant with the collection, the snapshot and encryption: age
```

This **replaces** the collection. Snapshot recovery does not require
stopping Qdrant, and runs inside the container, so no port needs publishing.
Qdrant recovers only from a snapshot file it can read itself, so the
decrypted snapshot exists in its volume while the recovery runs and is
deleted after it. Qdrant unpacks the snapshot before it replaces anything: a broken or
foreign one is refused with the collection untouched. The restore takes a
collection whose name has letters, digits and `._-` only.

The snapshot is streamed in rather than copied with `docker compose cp`: cp
keeps the host file's owner and mode, and Qdrant runs as root without
capabilities, so it cannot open a file it does not own — the recovery fails
with "Permission denied".

### Cadence

Default suggestion: **hourly** snapshots (cheap, online) with **24-hour**
local retention in `backup/snapshots/`, **30-day** retention off-host. The
volume keeps no snapshots; `backup/snapshots/` grows until you prune it.

## Scheduling

The schedule is deploy's (a systemd timer). It runs, in the data-plane
checkout:

```bash
make backup REASON="scheduled backup"
```

nightly, in the maintenance window (Neo4j stops for the dump), or
`make backup-neo4j REASON="scheduled backup"` nightly and `make backup-qdrant
REASON="scheduled backup"` hourly for the cadences above. `REASON=` is the
purpose the default `AUDIT_REQUIRE_PURPOSE` asks of a backup (`disclose`).
The unit runs as an account that may use Docker; the records name that
account as the operator. Exit status: `0` when the backup was written and
recorded; non-zero when it was refused (no usable recipients, no purpose,
admin-audit down), failed, or could not be recorded. The recipients file and
age must be in place before the first run.

## Plaintext from earlier versions

Up to 0.5, backups were written unencrypted: Neo4j dumps (`*.dump`, written
by a container, so owned by root) in `backup/snapshots/`, Qdrant snapshots in
the `qdrant-snapshots` volume and wherever they were copied. Every backup reports what is left, on
the host and in the volume, until it is gone; a restore takes only encrypted
files.

- To keep an unencrypted backup, encrypt it to the recipients, then delete
  the plaintext; it restores like any other:

  ```bash
  sudo age -R backup/recipients.txt -o backup/snapshots/<file>.age backup/snapshots/<file> \
    && sudo rm backup/snapshots/<file>
  ```

- The snapshots in the volume are replaced by a fresh `make backup`. Delete
  them in a recorded shell: `make qdrant-shell REASON="delete unencrypted
  snapshots"`, then `qdrant GET /collections/<collection>/snapshots` and
  `qdrant DELETE /collections/<collection>/snapshots/<snapshot>` for each.
- Copies made off-host earlier are unencrypted too: encrypt or delete them
  where they are.

## Verify-restore drill

A backup you have never restored is not a backup. Quarterly:

1. Spin up a scratch data-plane project (`docker compose -p data-plane-drill ...`).
2. Restore the most recent Neo4j dump and most recent Qdrant snapshot, each
   with a keyholder's identity: this also proves the identity still
   decrypts.
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
