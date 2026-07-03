# shellcheck shell=bash
# shellcheck disable=SC2034  # BUNDLE_VERSION/BUNDLE_BUILT/BUNDLE_PULLED/_BUNDLE_ORIG_REF are set for the sourcing bundle_images.sh (the last is read by the EXIT trap)
# bundle-lib.sh - shared helpers for nos-tromo airgap bundle scripts.
#
# Canonical source: nos-tromo/.github/configs/bundle/bundle-lib.sh
# Vendored verbatim into each repo at scripts/bundle-lib.sh; CI fails on drift
# (same pattern as make/common.mk). Source it from scripts/bundle_images.sh:
#   . scripts/bundle-lib.sh
#
# Provides:
#   bundle_version <repo-slug>
#       Compute the versioned image tag (commit date + short sha, or
#       <REPO_UC>_VERSION_OVERRIDE if set; falls back to today's date outside a
#       git repo). Exports <REPO_UC>_VERSION (compose interpolates it into image
#       tags), announces it on stdout, persists it to .<repo>-version, and
#       returns it via the global BUNDLE_VERSION. Call it directly (NOT in a
#       command substitution) so the export reaches the build/pull below:
#           bundle_version chorus; VER="$BUNDLE_VERSION"
#
#   bundle_retag <image-ref>            -> echoes the ref to save
#       If the ref is name:tag@digest, `docker tag name@digest name:tag` and
#       echo name:tag; otherwise echo the ref unchanged. Docker sometimes drops
#       the name:tag binding when an image is pulled as name:tag@digest, leaving
#       only the digest — re-tagging makes `docker save` produce a tarball that
#       loads back with both bindings, which compose's `image: name:tag@digest`
#       references need.
#
#   bundle_partition_images   (reads compose image refs on stdin)
#       For repos that BOTH build and pull: split into BUNDLE_BUILT[] (local
#       images, no registry slash) and BUNDLE_PULLED[] (registry refs, retagged
#       via bundle_retag). Feed via process substitution so the globals reach
#       the caller:  bundle_partition_images < <("${COMPOSE[@]}" config --images)
#
#   bundle_collect_pulled     (reads compose image refs on stdin)
#       For PULL-ONLY repos (no locally-built images, e.g. data-plane): every
#       ref is a pulled image (retagged via bundle_retag) -> BUNDLE_PULLED[].
#       Use this instead of bundle_partition_images, because official images
#       like `neo4j:tag@digest` carry no registry slash and must NOT be
#       misclassified as locally-built.

bundle_version() {
  local repo="$1" repo_uc override_var override _git_tag _git_sha _git_date _date ver
  repo_uc=$(printf '%s' "$repo" | tr 'a-z-' 'A-Z_')
  override_var="${repo_uc}_VERSION_OVERRIDE"
  override="${!override_var:-}"
  if [[ -n "$override" ]]; then
    ver="$override"
  else
    # Release: HEAD sits exactly on an annotated tag -> use it verbatim.
    # (`git describe --exact-match` without `--tags` considers ONLY annotated
    #  tags, so a stray lightweight tag can never become a release version.)
    _git_tag=$(git describe --exact-match HEAD 2>/dev/null || true)
    if [[ -n "$_git_tag" ]]; then
      ver="$_git_tag"
    else
      _git_sha=$(git rev-parse --short HEAD 2>/dev/null || true)
      _git_date=$(git log -1 --format=%cs 2>/dev/null || true)
      _date="${_git_date:-$(date +%Y-%m-%d)}"
      ver="${_date}${_git_sha:+-${_git_sha}}"
    fi
  fi
  export "${repo_uc}_VERSION=$ver"
  printf '%s=%s\n' "${repo_uc}_VERSION" "$ver"
  printf '%s\n' "$ver" > ".${repo}-version"
  BUNDLE_VERSION="$ver"
}

# bundle_checkout_release <repo-slug>
#     PRODUCTION bundle only (`make bundle`). Resolve the latest ANNOTATED tag
#     reachable from HEAD, check it out (detached) so the compose build below is
#     the tagged tree, and register an EXIT trap restoring the original branch.
#     Refuses (nonzero) rather than ever bundling an unversioned artifact:
#       - dirty *tracked* working tree (a checkout would clobber it), or
#       - no annotated tag reachable from HEAD.
#     An explicit <REPO_UC>_VERSION_OVERRIDE short-circuits it entirely (bundle
#     the working tree as-is) - the dev escape hatch shared with `make bundle-dev`.
#     Untracked / gitignored files (e.g. .env) are ignored by the dirty check and
#     preserved across the checkout.
#     Call from scripts/bundle_images.sh BEFORE the compose build, dev-guarded:
#       [[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release chorus
bundle_checkout_release() {
  local repo="$1" repo_uc override_var override tag orig_ref
  repo_uc=$(printf '%s' "$repo" | tr 'a-z-' 'A-Z_')
  override_var="${repo_uc}_VERSION_OVERRIDE"
  override="${!override_var:-}"

  # Dev escape hatch: an explicit override bundles the working tree as-is.
  [[ -n "$override" ]] && return 0

  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    printf 'bundle: not a git repository - cannot resolve a release tag.\n' >&2
    printf "        Use 'make bundle-dev' to bundle the current local state.\n" >&2
    return 1
  fi

  # Refuse a dirty *tracked* tree; untracked/gitignored files (.env) are fine.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    printf 'bundle: uncommitted changes to tracked files - refusing to check out a tag.\n' >&2
    printf "        Commit or stash them, or use 'make bundle-dev'.\n" >&2
    return 1
  fi

  # Latest ANNOTATED tag reachable from HEAD (no --tags => annotated-only,
  # matching bundle_version's --exact-match convention).
  tag=$(git describe --abbrev=0 HEAD 2>/dev/null || true)
  if [[ -z "$tag" ]]; then
    printf 'bundle: no annotated release tag reachable from HEAD.\n' >&2
    printf "        Tag a release ('git tag -a vX.Y.Z -m ...') or use 'make bundle-dev'.\n" >&2
    return 1
  fi

  # Where to return: branch name if on one, else the detached SHA.
  orig_ref=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse HEAD)
  # Restore on ANY exit of the sourcing script (build success or failure).
  # Deferred expansion of the global avoids re-quoting the ref into the trap.
  _BUNDLE_ORIG_REF="$orig_ref"
  trap 'git checkout --quiet "$_BUNDLE_ORIG_REF"' EXIT

  printf 'bundle: building release tag %s (restoring %s afterwards)\n' "$tag" "$orig_ref"
  git checkout --quiet "$tag"
}

bundle_retag() {
  local img="$1" name tag digest
  if [[ "$img" =~ ^(.+):([^@]+)@(sha256:[a-f0-9]+)$ ]]; then
    name="${BASH_REMATCH[1]}"
    tag="${BASH_REMATCH[2]}"
    digest="${BASH_REMATCH[3]}"
    docker tag "${name}@${digest}" "${name}:${tag}"
    printf '%s' "${name}:${tag}"
  else
    printf '%s' "$img"
  fi
}

bundle_partition_images() {
  BUNDLE_BUILT=()
  BUNDLE_PULLED=()
  local img
  while IFS= read -r img; do
    [[ -z "$img" ]] && continue
    if [[ "$img" == */* ]]; then
      BUNDLE_PULLED+=("$(bundle_retag "$img")")
    else
      BUNDLE_BUILT+=("$img")
    fi
  done
}

bundle_collect_pulled() {
  BUNDLE_PULLED=()
  local img
  while IFS= read -r img; do
    [[ -z "$img" ]] && continue
    BUNDLE_PULLED+=("$(bundle_retag "$img")")
  done
}
