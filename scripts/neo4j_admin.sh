#!/usr/bin/env bash
# Neo4j's operator commands: a cypher-shell session, the offline dump, and
# the load of a dump.
#
#   COMPOSE=<docker compose command> scripts/neo4j_admin.sh shell
#   COMPOSE=... BACKUP_DIR=<dir> BACKUP_RECIPIENTS=<file> scripts/neo4j_admin.sh dump
#   COMPOSE=... BACKUP_DIR=<dir> scripts/neo4j_admin.sh load <dump file in BACKUP_DIR>
#
# make db-shell, backup-neo4j and restore-neo4j run them through
# scripts/audit-admin.sh, which records each in the audit trail. Neo4j
# Community has no online backup or restore: dump and load stop the service,
# run neo4j-admin in a one-shot container on its volumes, and start it again,
# also when neo4j-admin failed or was interrupted. Both report facts to
# $AUDIT_FACTS (key=value lines) for the record.
#
# The dump never reaches the host's disk unencrypted: neo4j-admin writes it to
# stdout, and age encrypts it on the way to BACKUP_DIR, to the public keys in
# BACKUP_RECIPIENTS (scripts/backup_age.sh).
set -euo pipefail

usage() {
  echo "usage: COMPOSE=<docker compose command> [BACKUP_DIR=<dir>] [BACKUP_RECIPIENTS=<file>] $0 shell | dump | load <dump file>" >&2
  exit 2
}

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/backup_age.sh
. "$here/backup_age.sh"

read -r -a compose <<<"${COMPOSE:?COMPOSE must hold the docker compose command}"
facts=${AUDIT_FACTS:-/dev/null}
# Set while this script has Neo4j stopped, and to a file it is writing: the
# exit trap starts Neo4j again and removes the unfinished file.
stopped=0
partial=""

# fact KEY VALUE: one fact for the audit record.
fact() {
  printf '%s=%s\n' "$1" "$2" >>"$facts"
}

# fact_size FILE: its size as a fact.
fact_size() {
  local size
  size=$(file_size "$1") || return 0
  fact size_bytes "$size"
}

# neo4j_admin ARGS...: neo4j-admin in a one-shot container on Neo4j's volumes,
# without a terminal, so that its stdin and stdout can carry a dump.
neo4j_admin() {
  "${compose[@]}" run --rm -T --entrypoint neo4j-admin neo4j "$@"
}

# offline COMMAND...: run COMMAND while Neo4j is stopped, then start Neo4j
# again whatever COMMAND's outcome. Nothing runs when Neo4j cannot be stopped.
offline() {
  local status=0
  "${compose[@]}" stop neo4j || return
  stopped=1
  "$@" || status=$?
  if ! start_neo4j; then
    [[ $status -ne 0 ]] || status=1
  fi
  return "$status"
}

# start_neo4j: start Neo4j again after an offline command.
start_neo4j() {
  stopped=0
  if ! "${compose[@]}" start neo4j; then
    echo "ERROR: neo4j did not start again" >&2
    return 1
  fi
}

# cleanup: on any exit, remove an unfinished file and start Neo4j if this
# script stopped it (an interrupted dump or load).
cleanup() {
  if [[ -n $partial ]]; then
    rm -f "$partial"
  fi
  if [[ $stopped -eq 1 ]]; then
    start_neo4j || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# dump_to FILE: dump the database through age into FILE, by way of FILE.part,
# so that an unfinished dump never looks whole. A failure of either keeps
# nothing: age would close a cut-off dump as a complete file of its own.
dump_to() {
  local statuses
  partial="$1.part"
  neo4j_admin database dump neo4j --to-stdout </dev/null | age -R "$AGE_RECIPIENTS" -o "$partial"
  statuses=("${PIPESTATUS[@]}")
  if [[ ${statuses[0]} -ne 0 || ${statuses[1]} -ne 0 ]]; then
    echo "ERROR: the dump failed (neo4j-admin exit ${statuses[0]}, age exit ${statuses[1]}); nothing was kept" >&2
    rm -f "$partial"
    partial=""
    return 1
  fi
  mv "$partial" "$1" || return 1
  partial=""
}

case ${1:-} in
  shell)
    [[ $# -eq 1 ]] || usage
    # The login comes from the container's own NEO4J_AUTH, as in the
    # healthcheck, and reaches cypher-shell through its environment, never
    # its command line. Without a terminal (piped queries) no TTY is asked for.
    tty=()
    [[ -t 0 ]] || tty=(-T)
    # shellcheck disable=SC2016  # expanded by the container's shell, not here
    exec "${compose[@]}" exec ${tty[@]+"${tty[@]}"} neo4j bash -c \
      'NEO4J_USERNAME="${NEO4J_AUTH%%/*}" NEO4J_PASSWORD="${NEO4J_AUTH#*/}" exec cypher-shell'
    ;;
  dump)
    [[ $# -eq 1 ]] || usage
    age_recipients || exit 2
    dir=$(backup_dir)
    warn_plaintext "$dir"
    file="neo4j-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
    umask 077
    echo ">> stopping neo4j for an offline dump, encrypted with age to $AGE_RECIPIENT_COUNT recipient(s)"
    status=0
    offline dump_to "$dir/$file" || status=$?
    age_facts "$facts"
    if [[ -f "$dir/$file" ]]; then
      fact file "$file"
      fact_size "$dir/$file"
      echo ">> wrote ${BACKUP_DIR}/$file"
    fi
    exit "$status"
    ;;
  load)
    [[ $# -eq 2 && $2 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || usage
    dir=$(backup_dir)
    if [[ ! -f "$dir/$2" ]]; then
      echo "ERROR: no dump $2 in ${BACKUP_DIR}" >&2
      exit 2
    fi
    fact_size "$dir/$2"
    echo ">> stopping neo4j to load $2"
    # neo4j-admin loads <database>.dump from --from-path: mount the file as that.
    offline "${compose[@]}" run --rm -v "$dir/$2:/backup/neo4j.dump:ro" --entrypoint /bin/bash neo4j \
      -c "neo4j-admin database load neo4j --from-path=/backup --overwrite-destination=true" </dev/null
    echo ">> loaded $2 into the neo4j database"
    ;;
  *)
    usage
    ;;
esac
