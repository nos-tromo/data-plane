#!/usr/bin/env bash
# Open and close the admin overlay's loopback ports (docker/compose.admin.yaml)
# and record both in the audit trail (README: Admin access, Audit trail).
#
#   COMPOSE=<docker compose command> scripts/admin-ports.sh open -- <command publishing the ports ...>
#   COMPOSE=<docker compose command> scripts/admin-ports.sh close up|down
#   COMPOSE=<docker compose command> scripts/admin-ports.sh status
#
# open    (make up-admin) needs CASE_REF= or REASON=. Writes admin /
#         db.ports.open before the command publishes anything, and publishes
#         nothing while that cannot be written or a window is already open.
#         The open event's id and time go to the state file, .admin-ports
#         (ADMIN_PORTS_STATE), readable by the operator only. If the command
#         fails and no port is published, the window is closed at once.
# close   writes admin / db.ports.close, linked to that open event, with how
#         long the ports were open, then removes the state file: make up runs
#         it once the stores are back in their production shape, make down
#         before it removes them (`up` or `down` is recorded as what closed
#         the window). Nothing to do without a state file. When the close
#         cannot be recorded the state file stays, and the next make up
#         records it.
# status  (make health) says whether the stores publish ports, and since when.
#
# The operator is the sudo user, else the login user.
set -euo pipefail

usage() {
  echo "usage: COMPOSE=<docker compose command> $0 open -- <command ...> | close up|down | status" >&2
  exit 2
}

read -r -a compose <<<"${COMPOSE:?COMPOSE must hold the docker compose command}"
state=${ADMIN_PORTS_STATE:-.admin-ports}
AUDIT_CLI_USER="${SUDO_USER:-$(id -un)}"
AUDIT_CASE_REF="${CASE_REF:-}"
AUDIT_REASON="${REASON:-}"
export AUDIT_CLI_USER AUDIT_CASE_REF AUDIT_REASON
# As in audit-admin.sh: the purpose travels in the environment, and the
# records read no stdin.
audit=("${compose[@]}" exec -T -e AUDIT_CLI_USER -e AUDIT_CASE_REF -e AUDIT_REASON admin-audit python /app/admin_audit.py)

# published: the host bindings of the project's containers, one per line.
published() {
  "${compose[@]}" --profile cpu --profile cuda ps \
    --format '{{range .Publishers}}{{if .PublishedPort}}{{.URL}}:{{.PublishedPort}} {{end}}{{end}}' </dev/null 2>/dev/null |
    tr ' ' '\n' | sed '/^$/d' | sort -u
}

# read_state: OPENED and SINCE from the state file. Returns 1 when there is
# none, 2 when it is damaged.
read_state() {
  OPENED="" SINCE=""
  [[ -e $state ]] || return 1
  local key value
  while IFS='=' read -r key value; do
    case $key in
      opened) OPENED=$value ;;
      opened_at) SINCE=$value ;;
    esac
  done <"$state"
  if [[ ! $OPENED =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ || ! $SINCE =~ ^[0-9]+$ ]]; then
    echo "admin-ports: $state is damaged; once the ports are closed, record the close by other means and remove it." >&2
    return 2
  fi
}

# iso EPOCH: the time in UTC (GNU date, then BSD date).
iso() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ
}

# record_close BY STATUS: write db.ports.close for the open window.
record_close() {
  "${audit[@]}" end ports --status "$2" --duration-s "$(($(date +%s) - SINCE))" --opened "$OPENED" "by=$1" </dev/null
}

[[ $# -ge 1 ]] || usage
case $1 in
  open)
    [[ $# -ge 3 && $2 == -- ]] || usage
    shift 2
    rc=0
    read_state || rc=$?
    if [[ $rc -eq 0 ]]; then
      echo "The admin ports are already open (since $(iso "$SINCE")): close them with make up first." >&2
      exit 2
    fi
    [[ $rc -eq 1 ]] || exit 2
    status=0
    opened=$("${audit[@]}" begin ports </dev/null) || status=$?
    if [[ $status -eq 2 ]]; then
      exit 2 # refused; admin-audit said why
    elif [[ $status -ne 0 ]]; then
      echo "admin-audit cannot record the opening: start data-plane first (make up). No port was published." >&2
      exit 1
    fi
    SINCE=$(date +%s)
    OPENED=$opened
    tmp=$(mktemp "$state.XXXXXX") # private, and in the same directory, so the rename is atomic
    printf 'opened=%s\nopened_at=%s\n' "$OPENED" "$SINCE" >"$tmp"
    mv "$tmp" "$state"
    status=0
    trap 'status=130' INT TERM HUP
    "$@" || status=$?
    trap '' INT TERM HUP
    if [[ $status -ne 0 && -z $(published) ]]; then
      # Nothing was published: the window ends here, as a failed opening.
      if record_close up-admin "$status"; then
        rm -f "$state"
      else
        echo "The opening failed and its close could not be recorded: record it by other means." >&2
      fi
    fi
    exit "$status"
    ;;
  close)
    [[ $# -eq 2 && ($2 == up || $2 == down) ]] || usage
    rc=0
    read_state || rc=$?
    [[ $rc -ne 1 ]] || exit 0
    [[ $rc -eq 0 ]] || exit 2
    if record_close "$2" 0; then
      rm -f "$state"
      exit 0
    fi
    echo "The admin ports are closing, but their close could not be recorded: run make up again once admin-audit runs." >&2
    exit 1
    ;;
  status)
    [[ $# -eq 1 ]] || usage
    ports=$(published | paste -sd ' ' -)
    rc=0
    read_state || rc=$?
    if [[ -n $ports && $rc -eq 0 ]]; then
      echo "admin ports: OPEN since $(iso "$SINCE") ($ports); make up closes them"
    elif [[ -n $ports ]]; then
      echo "admin ports: OPEN ($ports) without an open record, so not by make up-admin; make up closes them"
    elif [[ $rc -eq 0 ]]; then
      echo "admin ports: closed, but the window opened $(iso "$SINCE") has no close record yet; make up records it"
    else
      echo "admin ports: closed"
    fi
    ;;
  *)
    usage
    ;;
esac
