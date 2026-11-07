#!/usr/bin/env bash
# Run a direct-access, backup or restore operation on a store and record it in
# the data-protection audit trail (README: Audit trail).
#
#   COMPOSE="docker compose --env-file .env -f docker/compose.yaml" \
#     scripts/audit-admin.sh <shell|backup|restore|prune> qdrant [key=value ...] -- <command ...>
#
# The operator is the sudo user, or the login user without sudo; CASE_REF and
# REASON in the environment state the purpose (make passes its variables on).
#
# 1. Asks admin-audit to admit the operation. It refuses (exit 2) when a
#    purpose is needed and none is stated, recording the refusal, or when the
#    stated one is malformed, and admits nothing while it cannot record
#    (exit 1). Nothing runs then. A shell's opening is recorded at this point.
# 2. Runs the command. It may report facts for the record (the file written,
#    its size, ...) as key=value lines in the file named by AUDIT_FACTS.
# 3. Records the outcome: ok when the command exited 0, else error (it may
#    have run in part), with its exit status, duration and facts. A shell's
#    close points at its opening. An interrupted command is recorded too.
#
# Exits with the command's status; 2 when refused; 1 when nothing could be
# recorded, or when the final record could not be written. The key=value
# parameters are recorded verbatim: never pass a secret.
set -euo pipefail

usage() {
  echo "usage: COMPOSE=<docker compose command> $0 <shell|backup|restore|prune> qdrant [key=value ...] -- <command ...>" >&2
  exit 2
}

[[ $# -ge 4 ]] || usage
kind=$1
store=$2
shift 2
params=()
while [[ $# -gt 0 && $1 != "--" ]]; do
  params+=("$1")
  shift
done
[[ $# -ge 2 ]] || usage
shift

read -r -a compose <<<"${COMPOSE:?COMPOSE must hold the docker compose command}"
AUDIT_CLI_USER="${SUDO_USER:-$(id -un)}"
AUDIT_CASE_REF="${CASE_REF:-}"
AUDIT_REASON="${REASON:-}"
export AUDIT_CLI_USER AUDIT_CASE_REF AUDIT_REASON
# Valueless -e: compose takes the values from this environment, so the purpose
# never shows on a command line. stdin is /dev/null for every record, so a
# command reading stdin (a piped qdrant-shell session) gets all of it.
audit=("${compose[@]}" exec -T -e AUDIT_CLI_USER -e AUDIT_CASE_REF -e AUDIT_REASON admin-audit python /app/admin_audit.py)

status=0
opened=$("${audit[@]}" begin "$kind" "$store" ${params[@]+"${params[@]}"} </dev/null) || status=$?
if [[ $status -eq 2 ]]; then
  exit 2 # refused; admin-audit said why
elif [[ $status -ne 0 ]]; then
  echo "admin-audit cannot record this operation: start data-plane first (make up). Nothing was done." >&2
  exit 1
fi

AUDIT_FACTS=$(mktemp "${TMPDIR:-/tmp}/data-plane-audit.XXXXXX")
export AUDIT_FACTS
trap 'rm -f "$AUDIT_FACTS"' EXIT

started=$SECONDS
status=0
trap 'status=130' INT
trap 'status=143' TERM
trap 'status=129' HUP
"$@" || status=$?
duration=$((SECONDS - started))

facts=()
while IFS= read -r line || [[ -n $line ]]; do
  [[ -z $line ]] || facts+=("$line")
done <"$AUDIT_FACTS"

# The record is written even if the operator interrupts again now.
trap '' INT TERM HUP
record=("${audit[@]}" end "$kind" "$store" --status "$status" --duration-s "$duration")
[[ -z $opened ]] || record+=(--opened "$opened")
if ! "${record[@]}" ${params[@]+"${params[@]}"} ${facts[@]+"${facts[@]}"} </dev/null; then
  echo "The operation ran (exit $status), but its audit record could not be written: record it by other means." >&2
  exit 1
fi
exit "$status"
