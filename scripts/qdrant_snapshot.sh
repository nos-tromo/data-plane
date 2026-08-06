#!/usr/bin/env bash
# Trigger an online snapshot for every Qdrant collection via the local API.
#
# Runs INSIDE the Qdrant container (piped over stdin by `make backup-qdrant`).
# The slim image ships no curl/wget/python3, so this speaks HTTP/1.0 over
# bash's built-in /dev/tcp redirection. HTTP/1.0 on purpose: the server then
# closes the connection and never chunk-encodes the body, so the response can
# be read with a plain `cat` and split on the blank line.
#
# Exits non-zero if listing collections fails or any snapshot POST fails.
set -euo pipefail

QDRANT_PORT="${QDRANT_PORT:-6333}"

# http METHOD PATH -> prints "STATUS<newline>BODY", using fd 3 for the socket.
http() {
  local method=$1 path=$2 response
  exec 3<>"/dev/tcp/localhost/${QDRANT_PORT}"
  printf '%s %s HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n' \
    "$method" "$path" >&3
  response=$(cat <&3)
  exec 3>&- 3<&-
  awk 'NR==1 {print $2}' <<<"$response"
  sed '1,/^\r*$/d' <<<"$response"
}

fail=0

list=$(http GET /collections)
if [[ $(head -n1 <<<"$list") != 200 ]]; then
  echo "ERROR: GET /collections returned HTTP $(head -n1 <<<"$list")" >&2
  exit 1
fi

collections=$(sed 1d <<<"$list" | grep -oE '"name":"[^"]+"' | cut -d'"' -f4 || true)
if [[ -z "$collections" ]]; then
  echo "  no collections to snapshot"
fi

while IFS= read -r col; do
  [[ -z "$col" ]] && continue
  echo "  snapshotting $col"
  out=$(http POST "/collections/${col}/snapshots")
  status=$(head -n1 <<<"$out")
  if [[ $status == 200 ]]; then
    echo "    -> $(sed 1d <<<"$out" | grep -oE '"name":"[^"]+"' | cut -d'"' -f4)"
  else
    echo "    ERROR: HTTP $status snapshotting $col" >&2
    fail=1
  fi
done <<<"$collections"

exit "$fail"
