#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s <immutable-image-tag> <full-40-character-revision>\n' "${0##*/}" >&2
}

if [ "$#" -ne 2 ]; then
  usage
  exit 64
fi

readonly image_tag="$1"
readonly revision="$2"

if [[ ! "$image_tag" =~ ^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'Image tag must be an explicit immutable repository:tag reference.\n' >&2
  exit 64
fi

readonly tag_name="${image_tag##*:}"
case "${tag_name,,}" in
  latest|production|stable|current)
    printf 'Mutable or environment-named image tags are not allowed.\n' >&2
    exit 64
    ;;
esac

if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Revision must be a full 40-character lowercase Git SHA.\n' >&2
  exit 64
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
repo_root="$(cd -- "$script_dir/../.." && pwd)"
readonly repo_root
readonly compose_file="$script_dir/docker-compose.build.yml"

if [ ! -f "$compose_file" ] || [ ! -f "$repo_root/Dockerfile" ]; then
  printf 'Build-only Compose contract or Dockerfile is missing.\n' >&2
  exit 66
fi

current_revision="$(git -C "$repo_root" rev-parse --verify HEAD)"
readonly current_revision
if [ "$current_revision" != "$revision" ]; then
  printf 'Requested revision does not match the build worktree HEAD.\n' >&2
  exit 65
fi

if [ -n "$(git -C "$repo_root" status --porcelain=v1)" ]; then
  printf 'Build worktree must be clean.\n' >&2
  exit 65
fi

COMPOSE_DISABLE_ENV_FILE=1 \
HERMES_IMAGE="$image_tag" \
HERMES_GIT_SHA="$revision" \
docker compose \
  --env-file /dev/null \
  --project-directory "$repo_root" \
  -f "$compose_file" \
  --project-name hermes-build \
  build \
  hermes-bot
