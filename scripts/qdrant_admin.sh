#!/usr/bin/env bash
# Qdrant's operator commands: an API shell, the online snapshot of every
# collection, and the recovery of one collection from a snapshot.
#
#   COMPOSE=<docker compose command> [PROFILE=cpu|cuda] scripts/qdrant_admin.sh shell
#   COMPOSE=... BACKUP_DIR=<dir> BACKUP_RECIPIENTS=<file> scripts/qdrant_admin.sh snapshot
#   COMPOSE=... BACKUP_DIR=<dir> scripts/qdrant_admin.sh recover <collection> <snapshot file>
#
# make qdrant-shell, backup-qdrant and restore-qdrant run them through
# scripts/audit-admin.sh, which records each in the audit trail. They work
# inside the running Qdrant container (qdrant-$PROFILE), which ships bash but
# no HTTP client: scripts/qdrant_api.sh speaks HTTP over bash's /dev/tcp, so
# no port needs publishing.
#
# snapshot takes the snapshots one collection at a time. Qdrant can only write
# a snapshot into its own volume, so each is streamed out of the container
# through age into BACKUP_DIR, encrypted to the public keys in
# BACKUP_RECIPIENTS (scripts/backup_age.sh), and deleted from the volume at
# once, also when the copy failed or was interrupted: no unencrypted snapshot
# stays behind.
#
# recover takes the snapshot from BACKUP_DIR when it is there (a copy brought
# back from off-host), copying it into the container first; otherwise from
# the qdrant-snapshots volume. Facts for the audit record go to $AUDIT_FACTS
# (key=value lines).
set -euo pipefail

usage() {
  echo "usage: COMPOSE=<docker compose command> [PROFILE=cpu|cuda] [BACKUP_DIR=<dir>] [BACKUP_RECIPIENTS=<file>] $0 shell | snapshot | recover <collection> <snapshot file>" >&2
  exit 2
}

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/backup_age.sh
. "$here/backup_age.sh"

read -r -a compose <<<"${COMPOSE:?COMPOSE must hold the docker compose command}"
service="qdrant-${PROFILE:-cpu}"
facts=${AUDIT_FACTS:-/dev/null}
safe='^[A-Za-z0-9_-][A-Za-z0-9._-]*$'
# While a snapshot is on its way out: its collection and name in the volume,
# and the host file being written. The exit trap deletes both.
pending_collection=""
pending_snapshot=""
partial=""

# api COMMAND [ARG...]: scripts/qdrant_api.sh in the Qdrant container.
api() {
  "${compose[@]}" exec -T "$service" bash -c "$(cat "$here/qdrant_api.sh")" qdrant_api "$@" </dev/null
}

# drop_pending: delete the snapshot on its way out from the volume. Returns 1
# when it is still there.
drop_pending() {
  local collection=$pending_collection snapshot=$pending_snapshot
  pending_collection=""
  pending_snapshot=""
  if ! api drop-snapshot "$collection" "$snapshot" >/dev/null; then
    echo "    ERROR: the unencrypted snapshot $snapshot of $collection is still in the qdrant-snapshots volume:" >&2
    echo "           delete it (backup/README.md: Plaintext from earlier versions)" >&2
    return 1
  fi
}

# cleanup: on any exit, delete a snapshot still on its way out and the
# unfinished host file (an interrupted backup).
cleanup() {
  if [[ -n $partial ]]; then
    rm -f "$partial"
  fi
  if [[ -n $pending_snapshot ]]; then
    drop_pending || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# host_name COLLECTION: the file in BACKUP_DIR for its encrypted snapshot,
# qdrant-<collection>-<stamp>.snapshot.age. Characters a file name should not
# carry become _, and a name already taken gets a number.
host_name() {
  local LC_ALL=C base name n=1
  base=${1//[^A-Za-z0-9._-]/_}
  base=${base//../__}
  base=${base:0:180}
  name="qdrant-$base-$stamp.snapshot.age"
  while [[ -e $dir/$name || -e $dir/$name.part ]]; do
    n=$((n + 1))
    name="qdrant-$base-$stamp-$n.snapshot.age"
  done
  printf '%s\n' "$name"
}

# snapshot_one COLLECTION: snapshot it, stream the snapshot out of the
# container through age into BACKUP_DIR, and delete it from the volume again.
# Counts what was written, failed, or kept in the volume.
snapshot_one() {
  local collection=$1 name file statuses size
  echo "  snapshotting $collection" >&2
  if ! name=$(api snapshot "$collection"); then
    failed=$((failed + 1))
    return 1
  fi
  pending_collection=$collection
  pending_snapshot=$name
  file=$(host_name "$collection")
  partial="$dir/$file.part"
  "${compose[@]}" exec -T "$service" cat "/qdrant/snapshots/$collection/$name" </dev/null |
    age -R "$AGE_RECIPIENTS" -o "$partial"
  statuses=("${PIPESTATUS[@]}")
  if ! drop_pending; then
    kept=$((kept + 1))
  fi
  if [[ ${statuses[0]} -ne 0 || ${statuses[1]} -ne 0 ]]; then
    echo "    ERROR: the snapshot of $collection could not be copied out encrypted (exit ${statuses[0]}, age exit ${statuses[1]})" >&2
    rm -f "$partial"
    partial=""
    failed=$((failed + 1))
    return 1
  fi
  mv "$partial" "$dir/$file" || return 1
  partial=""
  size=$(file_size "$dir/$file") || size=0
  echo "    -> ${BACKUP_DIR}/$file" >&2
  printf 'collection=%s\n' "$collection" >>"$facts"
  written=$((written + 1))
  total=$((total + size))
}

# leftovers: how many files the qdrant-snapshots volume holds: snapshots an
# earlier version left there, or one that could not be deleted.
leftovers() {
  # shellcheck disable=SC2016  # expanded by the container's shell
  "${compose[@]}" exec -T "$service" bash -c \
    'shopt -s globstar dotglob nullglob; n=0; for f in /qdrant/snapshots/**; do if [[ -f $f ]]; then n=$((n + 1)); fi; done; echo "$n"' </dev/null
}

case ${1:-} in
  shell)
    [[ $# -eq 1 ]] || usage
    tty=()
    [[ -t 0 ]] || tty=(-T)
    exec "${compose[@]}" exec ${tty[@]+"${tty[@]}"} "$service" bash -c "$(cat "$here/qdrant_api.sh")" qdrant_api shell
    ;;
  snapshot)
    [[ $# -eq 1 ]] || usage
    age_recipients || exit 2
    if ! "${compose[@]}" exec -T "$service" sh -c 'command -v bash >/dev/null 2>&1' </dev/null; then
      echo "ERROR: qdrant image has no bash — snapshot NOT taken." >&2
      echo "       See backup/README.md." >&2
      exit 1
    fi
    dir=$(backup_dir)
    warn_plaintext "$dir"
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    umask 077
    echo ">> snapshotting every collection, encrypted with age to $AGE_RECIPIENT_COUNT recipient(s)"
    age_facts "$facts"
    if ! collections=$(api collections); then
      echo "ERROR: the collections could not be listed; no snapshot was taken" >&2
      exit 1
    fi
    if [[ -z $collections ]]; then
      echo "  no collections to snapshot" >&2
    fi
    written=0
    total=0
    failed=0
    kept=0
    while IFS= read -r collection <&3; do
      [[ -n $collection ]] || continue
      snapshot_one "$collection" || true
    done 3<<<"$collections"
    printf 'snapshots=%s\nsize_bytes=%s\nfailed=%s\n' "$written" "$total" "$failed" >>"$facts"
    left=$(leftovers) || left=""
    if [[ $left =~ ^[0-9]+$ && $left -gt 0 ]]; then
      echo "WARNING: $left unencrypted file(s) remain in the qdrant-snapshots volume:" >&2
      echo "         delete them (backup/README.md: Plaintext from earlier versions)." >&2
    fi
    echo ">> encrypted snapshots are in ${BACKUP_DIR}"
    [[ $failed -eq 0 && $kept -eq 0 ]]
    ;;
  recover)
    [[ $# -eq 3 && $2 =~ $safe && $3 =~ $safe && $3 != *..* ]] || usage
    collection=$2
    snapshot=$3
    path="/qdrant/snapshots/$collection/$snapshot"
    if [[ -n ${BACKUP_DIR:-} && -f "$BACKUP_DIR/$snapshot" ]]; then
      echo ">> copying $snapshot from ${BACKUP_DIR} into the Qdrant container"
      # Streamed in, not `docker compose cp`: cp keeps the host file's owner
      # and mode, and Qdrant (root without capabilities) cannot read a file
      # it does not own. Private like Qdrant's own snapshots; the rename
      # keeps a broken copy from looking whole.
      # shellcheck disable=SC2016  # expanded by the container's shell
      "${compose[@]}" exec -T "$service" sh -c 'umask 077 && mkdir -p "${1%/*}" && cat >"$1.part" && mv "$1.part" "$1"' \
        _ "$path" <"$BACKUP_DIR/$snapshot"
      printf 'source=host\n' >>"$facts"
    else
      printf 'source=volume\n' >>"$facts"
    fi
    echo ">> recovering collection $collection from $snapshot"
    api recover "$collection" "$path"
    ;;
  *)
    usage
    ;;
esac
