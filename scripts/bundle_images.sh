#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091,SC2154  # sources vendored scripts/bundle-lib.sh (sets BUNDLE_*)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
. scripts/bundle-lib.sh

PROFILE="${1:-cpu}"
bundle_version data-plane; VER="$BUNDLE_VERSION"

COMPOSE=(docker compose --env-file .env -f docker/compose.yaml)
"${COMPOSE[@]}" --profile "$PROFILE" pull
bundle_collect_pulled < <("${COMPOSE[@]}" --profile "$PROFILE" config --images)

if (( ${#BUNDLE_PULLED[@]} == 0 )); then
  echo "No images resolved for profile '$PROFILE'." >&2
  exit 1
fi
echo "Saving images: ${BUNDLE_PULLED[*]}"
docker save "${BUNDLE_PULLED[@]}" | gzip > "data-plane-pulled-${PROFILE}-${VER}.tar.gz"
echo "Wrote: data-plane-pulled-${PROFILE}-${VER}.tar.gz"
