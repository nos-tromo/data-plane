#!/usr/bin/env bash
# Neo4j's operator commands: a cypher-shell session, the offline dump, and
# the load of a dump.
#
#   COMPOSE=<docker compose command> scripts/neo4j_admin.sh shell
#   COMPOSE=... BACKUP_DIR=<dir> scripts/neo4j_admin.sh dump
#   COMPOSE=... BACKUP_DIR=<dir> scripts/neo4j_admin.sh load <dump file in BACKUP_DIR>
#
# make db-shell, backup-neo4j and restore-neo4j run them through
# scripts/audit-admin.sh, which records each in the audit trail. Neo4j
# Community has no online backup or restore: dump and load stop the service,
# run neo4j-admin in a one-shot container on its volumes, and start it again,
# also when neo4j-admin failed. Both report the dump's name and size to
# $AUDIT_FACTS (key=value lines) for the record.
set -euo pipefail

usage() {
  echo "usage: COMPOSE=<docker compose command> [BACKUP_DIR=<dir>] $0 shell | dump | load <dump file>" >&2
  exit 2
}

read -r -a compose <<<"${COMPOSE:?COMPOSE must hold the docker compose command}"
facts=${AUDIT_FACTS:-/dev/null}

# fact KEY VALUE: one fact for the audit record.
fact() {
  printf '%s=%s\n' "$1" "$2" >>"$facts"
}

# fact_size FILE: its size as a fact. stat needs no read permission on the
# file (a dump the container wrote as root); GNU stat first, then BSD.
fact_size() {
  local size
  size=$(stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null) || return 0
  fact size_bytes "$size"
}

# backup_dir: the absolute backup directory, created on demand.
backup_dir() {
  local dir=${BACKUP_DIR:?BACKUP_DIR must name the backup directory}
  mkdir -p "$dir"
  (cd "$dir" && pwd)
}

# offline COMMAND: run COMMAND with neo4j-admin in a one-shot container while
# Neo4j is stopped, then start Neo4j again whatever COMMAND's outcome. Nothing
# runs when Neo4j cannot be stopped. Extra docker compose run options are in
# OFFLINE_OPTS.
offline() {
  local status=0
  "${compose[@]}" stop neo4j || return
  "${compose[@]}" run --rm ${OFFLINE_OPTS[@]+"${OFFLINE_OPTS[@]}"} --entrypoint /bin/bash neo4j -c "$1" </dev/null ||
    status=$?
  if ! "${compose[@]}" start neo4j; then
    echo "ERROR: neo4j did not start again" >&2
    [[ $status -ne 0 ]] || status=1
  fi
  return "$status"
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
    dir=$(backup_dir)
    file="neo4j-$(date -u +%Y%m%dT%H%M%SZ).dump"
    echo ">> stopping neo4j for offline dump"
    OFFLINE_OPTS=(-v "$dir:/backup")
    status=0
    offline "neo4j-admin database dump neo4j --to-path=/backup --overwrite-destination=true && mv /backup/neo4j.dump /backup/$file" ||
      status=$?
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
    OFFLINE_OPTS=(-v "$dir/$2:/backup/neo4j.dump:ro")
    offline "neo4j-admin database load neo4j --from-path=/backup --overwrite-destination=true"
    echo ">> loaded $2 into the neo4j database"
    ;;
  *)
    usage
    ;;
esac
