#!/usr/bin/env bash
set -euo pipefail

# data-plane airgap bundler.
#
# data-plane builds no images of its own — Neo4j and Qdrant are digest-pinned
# upstream images — so this pulls them for the chosen profile and saves them as
# one versioned tarball to copy to an offline host alongside compose.yaml and
# .env. Load it there with `docker load -i <tarball>` before `make up`.

PROFILE="${1:-cpu}"
COMPOSE="docker compose --env-file .env -f docker/compose.yaml"

# Compute a version from git (commit date + short sha), falling back to today's
# date outside a git repo. Override by exporting DATA_PLANE_VERSION_OVERRIDE
# before invoking make.
if [[ -n "${DATA_PLANE_VERSION_OVERRIDE:-}" ]]; then
  DATA_PLANE_VERSION="$DATA_PLANE_VERSION_OVERRIDE"
else
  _git_sha=$(git rev-parse --short HEAD 2>/dev/null || true)
  _git_date=$(git log -1 --format=%cs 2>/dev/null || true)
  _date="${_git_date:-$(date +%Y-%m-%d)}"
  DATA_PLANE_VERSION="${_date}${_git_sha:+-${_git_sha}}"
fi
echo "DATA_PLANE_VERSION=$DATA_PLANE_VERSION"

# Persist the version so an operator can read it off the file on the offline
# host. Copy this file alongside compose.yaml.
echo "$DATA_PLANE_VERSION" > .data-plane-version

# Pull the upstream images for the chosen profile (neo4j + qdrant-<profile>).
$COMPOSE --profile "$PROFILE" pull

# Collect the image refs. Docker sometimes drops the name:tag binding when an
# image is pulled as name:tag@digest, leaving only the digest — re-tag so the
# saved tarball loads back with both bindings, which compose's
# `image: name:tag@digest` references need.
pulled=()
while IFS= read -r img; do
  [[ -z "$img" ]] && continue
  if [[ "$img" =~ ^(.+):([^@]+)@(sha256:[a-f0-9]+)$ ]]; then
    name="${BASH_REMATCH[1]}"
    tag="${BASH_REMATCH[2]}"
    digest="${BASH_REMATCH[3]}"
    docker tag "${name}@${digest}" "${name}:${tag}"
    pulled+=("${name}:${tag}")
  else
    pulled+=("$img")
  fi
done < <($COMPOSE --profile "$PROFILE" config --images)

if (( ${#pulled[@]} == 0 )); then
  echo "No images resolved for profile '$PROFILE'." >&2
  exit 1
fi

echo "Saving images: ${pulled[*]}"
docker save "${pulled[@]}" | gzip > "data-plane-pulled-${PROFILE}-${DATA_PLANE_VERSION}.tar.gz"
echo "Wrote: data-plane-pulled-${PROFILE}-${DATA_PLANE_VERSION}.tar.gz"
