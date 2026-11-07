# Backup + restore runbook

Backups for Qdrant, the stateful service this project owns.

Every backup is encrypted with [age](https://age-encryption.org) on its way
out of the store, to the public keys in a recipients file. The identity that
decrypts stays offline; a restore takes it for that run only. No unencrypted
snapshot rests on the host: each snapshot is streamed out of the Qdrant
container through age and deleted from Qdrant's volume at once, and a restore
decrypts straight into the store, never to a file.

Every backup and restore target is recorded in the data-protection audit
trail by `admin-audit` (README § Audit trail): a backup as `disclose` /
`backup.qdrant` to `file:backup`, with its encryption and how many public
keys can read it, a restore as `modify` / `restore.qdrant`. They refuse to
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
private key, or has a line age does not take as a public key.

Whoever can change the file chooses who reads the next backup. So the file
and every directory above it, up to the checkout (or up to `/` for a file
outside it), must belong to root or to the account running the backup, and
be writable by their owner only. A directory with the sticky bit, such as
`/tmp`, may be writable by others. A backup also refuses a symbolic link in
place of the file, and a mode or owner it cannot read. It then reads the
file once, into a private copy that age encrypts to, so the keys its record
counts and fingerprints are the keys the backup was encrypted to. A key from
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
    qdrant-<collection>-<ISO8601-utc>.snapshot.age
    .prune-since         when pruning took effect on this host (Retention, below)
```

The files belong to whoever ran the backup, mode `0600`. Copy `snapshots/`
off-host as soon as the backup finishes: this directory is *staging*, not
durable storage. The copies stay encrypted; keep the identity apart from them.
Every backup deletes the backups older than the retention period
afterwards ([Retention](#retention)); the off-host copies are pruned at the
same age by the operator.

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

Default suggestion: **hourly** snapshots (cheap, online). Each run deletes the
snapshots older than `BACKUP_RETENTION_DAYS` (default 30) in
`backup/snapshots/`; keep the off-host copies no longer
([Retention](#retention)). The volume keeps no snapshots.

## Retention

Backups are short-lived restore points, not an archive: the data in the
store has a retention period of its own, and a backup kept longer would keep
what the store has already deleted. After every backup that succeeded,
`make backup-qdrant` (and with it `make backup`) deletes the backups older
than `BACKUP_RETENTION_DAYS` days (default `30`, whole days from 1 to 365,
in `.env` or on the command line); `make backup-prune` does it on its own. A
backup that failed prunes nothing, and so does a backup in which a
collection failed: the old snapshots stay the last restore point of that
collection until a backup succeeds again.

- **Which files.** A backup's age is the UTC time in its name.
  - Encrypted backups (`*.age`) and unencrypted ones from earlier versions
    follow the same rule.
  - Never deleted, only named on the terminal: unfinished files (`.part`),
    names without a time or that the record cannot carry (anything but
    letters, digits and `._-`), and times in the future (check the host's
    clock).
  - A file the backup account cannot delete (one root wrote) is named and
    counted as not deleted; remove it as root.
- **Grace.** A period that is new on the host takes effect when a pruning
  first sees it, and nothing is deleted for `RETENTION_NOTICE_DAYS` (default
  30) after that, so nothing an earlier version left is deleted without
  notice.
- **Changing the period** never deletes sooner than announced. A longer period
  applies at once (within a running first grace, at that grace's end), also to
  backups that were due under the shorter one but not yet pruned. A shorter one
  gives the notice period only to the backups it makes due sooner, never
  ending before a grace already running; meanwhile what the rule in force
  deletes still goes when it says. Changing `RETENTION_NOTICE_DAYS` alone
  moves nothing: it only sets the length of the next grace.
- **State.** `snapshots/.prune-since` holds when the period took effect, the
  period, when its grace ends and, after a change, the rule in force until
  then (mode `0644`, so every account that backs up can read it). The state
  of 0.7.0 is rewritten with its grace end on the first run. A state dated in
  the future (a clock set back) is warned about and its dates are capped at
  now, never restarted; one
  with impossible values (a period of 0 days, a grace end before its start) is
  refused and nothing is deleted. Deleting the file restarts the grace period.
- **Off-host copies** are backups too. This repo cannot reach them: prune them
  at the same age, on the medium they are kept on.
- **Recorded.** Each pruning is `delete` / `backup.prune.qdrant` in the audit
  trail, attributed like the backup that ran it, with the files deleted (by
  name), the period, the cutoff in effect and the counts; while a grace runs,
  also when it ends and, after a change, the period still in force
  (`grace_until`, `in_force_days`; README: Audit trail). A file another
  pruning removed first is not counted.
- **After a restore,** the restored data still carries its own deadlines: let
  docint's daily retention sweep run (it starts a few minutes after docint's
  backend starts) before users work on restored collections.

## Scheduling

The schedule is deploy's (a systemd timer). It runs, in the data-plane
checkout:

```bash
make backup REASON="scheduled backup"
```

nightly, or hourly for the cadence above: the snapshots are online, so
neither needs a maintenance window. `REASON=` is the purpose the default
`AUDIT_REQUIRE_PURPOSE` asks of a backup (`disclose`).
The unit runs as an account that may use Docker; the records name that
account as the operator. Exit status: `0` when the backup was written and
recorded; non-zero when it was refused (no usable recipients, no purpose,
admin-audit down), failed, or could not be recorded. The recipients file and
age must be in place before the first run, and the checkout, `backup/` and
the recipients file must belong to root or to that account, writable by
their owner only (The recipients file, above).

## Plaintext from earlier versions

Up to 0.5, backups were written unencrypted: Qdrant snapshots in the
`qdrant-snapshots` volume and wherever they were copied. Every backup reports
what is left, on the host and in the volume, until it is gone; a restore takes
only encrypted files.

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
2. Restore the most recent Qdrant snapshot with a keyholder's identity: this
   also proves the identity still decrypts.
3. Run docint's health endpoint against the drill stack.
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
