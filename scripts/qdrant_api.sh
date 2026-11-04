#!/usr/bin/env bash
# Qdrant's HTTP API from inside the Qdrant container. The images ship bash but
# no HTTP client, so this speaks HTTP/1.0 over bash's /dev/tcp, as the
# healthcheck and qdrant_snapshot.sh do. scripts/qdrant_admin.sh runs it in
# the container as `bash -c "$(cat scripts/qdrant_api.sh)" qdrant_api <command>`:
#
#   shell                             an interactive bash with `qdrant METHOD /path [JSON]`
#   recover <collection> <snapshot>   recover a collection from a snapshot file
#                                     under /qdrant/snapshots
#
# The api-key header carries the server's own key from this container's
# environment (QDRANT__SERVICE__API_KEY, set from .env's QDRANT_API_KEY);
# absent or empty, none is sent.
set -uo pipefail

# key_note [short]: explain the 401s an empty key causes. With QDRANT_API_KEY
# unset in .env, compose still hands Qdrant the variable, empty; Qdrant then
# answers every request that carries no api-key header with 401 (only
# /readyz, /healthz and /livez stay open), so clients that send no key are
# refused. The full note opens the shell, the short one follows each 401.
key_note() {
  [[ ${QDRANT__SERVICE__API_KEY+set} == set && -z $QDRANT__SERVICE__API_KEY ]] || return 0
  if [[ ${1:-} == short ]]; then
    echo "NOTE: 401 because this Qdrant was started with an empty API key: set QDRANT_API_KEY in .env and run make up again." >&2
    return 0
  fi
  echo "NOTE: this Qdrant was started with an empty API key (QDRANT_API_KEY unset or empty in .env)." >&2
  echo "      It answers every request without an api-key header with 401 Unauthorized, so clients that" >&2
  echo "      send no key are refused; only /readyz, /healthz and /livez stay open. Set QDRANT_API_KEY" >&2
  echo "      in .env (docint needs the same value) and run make up again." >&2
}

# qdrant METHOD /path [JSON]: one request to the local API. Prints the status
# line and the body; returns 0 for a 2xx answer, 1 otherwise.
qdrant() {
  local method=${1:-} path=${2:-} body=${3:-} key="" length response status
  if [[ -z $method || $path != /* ]]; then
    echo "usage: qdrant METHOD /path [JSON]   e.g. qdrant GET /collections" >&2
    return 2
  fi
  # ANSI-C quoting, not $(printf ...): command substitution would strip the
  # newline and leave a bare CR, which Qdrant rejects.
  if [[ -n ${QDRANT__SERVICE__API_KEY:-} ]]; then
    key="api-key: ${QDRANT__SERVICE__API_KEY}"$'\r\n'
  fi
  length=$(LC_ALL=C && printf '%s' "${#body}")
  exec 3<>"/dev/tcp/localhost/${QDRANT_PORT:-6333}" || return 1
  printf '%s %s HTTP/1.0\r\nHost: localhost\r\n%sContent-Type: application/json\r\nContent-Length: %s\r\n\r\n%s' \
    "$method" "$path" "$key" "$length" "$body" >&3
  response=$(cat <&3)
  exec 3>&- 3<&-
  status=$(awk 'NR==1 {print $2}' <<<"$response")
  head -n1 <<<"$response" | tr -d '\r'
  sed '1,/^\r*$/d' <<<"$response"
  [[ $status != 401 ]] || key_note short
  [[ $status == 2?? ]]
}

# qdrant_recover COLLECTION PATH: replace COLLECTION with the snapshot at PATH.
qdrant_recover() {
  local collection=$1 path=$2
  if [[ ! $collection =~ ^[A-Za-z0-9_-][A-Za-z0-9._-]*$ || $path != /qdrant/snapshots/* || $path == *..* ]]; then
    echo "ERROR: collection names and snapshot paths are letters, digits and ._- under /qdrant/snapshots" >&2
    return 2
  fi
  if [[ ! -f $path ]]; then
    echo "ERROR: no snapshot at $path" >&2
    return 1
  fi
  qdrant PUT "/collections/${collection}/snapshots/recover" "{\"location\":\"file://${path}\"}"
}

case ${1:-} in
  shell)
    key_note
    export -f qdrant key_note
    if [[ ! -t 0 ]]; then
      exec bash --norc -s # commands piped in: no prompt, no job control
    fi
    echo "Qdrant API shell: qdrant METHOD /path [JSON], e.g. qdrant GET /collections. exit closes it." >&2
    PS1='qdrant> ' exec bash --norc -i
    ;;
  recover)
    [[ $# -eq 3 ]] || {
      echo "usage: qdrant_api recover <collection> <snapshot path>" >&2
      exit 2
    }
    qdrant_recover "$2" "$3"
    ;;
  *)
    echo "usage: qdrant_api shell | recover <collection> <snapshot path>" >&2
    exit 2
    ;;
esac
