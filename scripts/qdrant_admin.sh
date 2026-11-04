#!/usr/bin/env bash
# Qdrant's operator commands: an API shell, the online snapshot of every
# collection, and the recovery of one collection from a snapshot.
#
#   COMPOSE=<docker compose command> [PROFILE=cpu|cuda] scripts/qdrant_admin.sh shell
#   COMPOSE=... scripts/qdrant_admin.sh snapshot
#   COMPOSE=... BACKUP_DIR=<dir> scripts/qdrant_admin.sh recover <collection> <snapshot file>
#
# make qdrant-shell, backup-qdrant and restore-qdrant run them through
# scripts/audit-admin.sh, which records each in the audit trail. They work
# inside the running Qdrant container (qdrant-$PROFILE), which ships bash but
# no HTTP client: scripts/qdrant_snapshot.sh and scripts/qdrant_api.sh speak
# HTTP over bash's /dev/tcp, so no port needs publishing.
#
# recover takes the snapshot from BACKUP_DIR when it is there (a copy brought
# back from off-host), copying it into the container first; otherwise from
# the qdrant-snapshots volume, where backup-qdrant leaves its snapshots.
# Facts for the audit record go to $AUDIT_FACTS (key=value lines).
set -euo pipefail

usage() {
  echo "usage: COMPOSE=<docker compose command> [PROFILE=cpu|cuda] [BACKUP_DIR=<dir>] $0 shell | snapshot | recover <collection> <snapshot file>" >&2
  exit 2
}

read -r -a compose <<<"${COMPOSE:?COMPOSE must hold the docker compose command}"
service="qdrant-${PROFILE:-cpu}"
facts=${AUDIT_FACTS:-/dev/null}
here=$(cd "$(dirname "$0")" && pwd)
safe='^[A-Za-z0-9_-][A-Za-z0-9._-]*$'

case ${1:-} in
  shell)
    [[ $# -eq 1 ]] || usage
    tty=()
    [[ -t 0 ]] || tty=(-T)
    exec "${compose[@]}" exec ${tty[@]+"${tty[@]}"} "$service" bash -c "$(cat "$here/qdrant_api.sh")" qdrant_api shell
    ;;
  snapshot)
    [[ $# -eq 1 ]] || usage
    if ! "${compose[@]}" exec -T "$service" sh -c 'command -v bash >/dev/null 2>&1' </dev/null; then
      echo "ERROR: qdrant image has no bash — snapshot NOT taken." >&2
      echo "       See backup/README.md for the host-side alternative." >&2
      exit 1
    fi
    echo ">> triggering qdrant snapshot for every collection"
    # The script reports its facts on stdout and its progress on stderr.
    "${compose[@]}" exec -T "$service" bash -s <"$here/qdrant_snapshot.sh" >>"$facts"
    echo ">> snapshots live in the qdrant-snapshots volume"
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
    "${compose[@]}" exec -T "$service" bash -c "$(cat "$here/qdrant_api.sh")" qdrant_api recover "$collection" "$path" </dev/null
    ;;
  *)
    usage
    ;;
esac
