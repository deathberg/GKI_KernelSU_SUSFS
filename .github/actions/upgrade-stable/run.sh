#!/usr/bin/env bash
set -euo pipefail

: "${STABLE_COMMIT:=}"
: "${STABLE_REPOSITORY:=https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git}"
: "${BASE_REPOSITORY:=}"
: "${REQUIRED_SUBLEVEL:=188}"
: "${GITHUB_ENV:=$GITHUB_WORKSPACE/kernel/stable-provenance/github-env}"
: "${GITHUB_OUTPUT:=$GITHUB_WORKSPACE/kernel/stable-provenance/github-output}"

if [ -z "$STABLE_COMMIT" ]; then
  exit 0
fi
if [[ ! "$STABLE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: stable_commit must be a full 40-character SHA" >&2
  exit 2
fi

KERNEL_DIR="${KERNEL_DIR:-$GITHUB_WORKSPACE/kernel/common}"
PROVENANCE_DIR="${PROVENANCE_DIR:-$GITHUB_WORKSPACE/kernel/stable-provenance}"
[ -d "$KERNEL_DIR/.git" ] || { echo "ERROR: $KERNEL_DIR is not a git checkout" >&2; exit 1; }

BASE_COMMIT="$(git -C "$KERNEL_DIR" rev-parse HEAD)"
if [ -n "$BASE_REPOSITORY" ]; then
  BASE_REPO="$BASE_REPOSITORY"
elif BASE_REPO="$(git -C "$KERNEL_DIR" remote get-url aosp 2>/dev/null)"; then
  :
elif BASE_REPO="$(git -C "$KERNEL_DIR" remote get-url origin 2>/dev/null)"; then
  :
else
  BASE_REPO="https://android.googlesource.com/kernel/common"
fi

git -C "$KERNEL_DIR" config user.name 'github-actions[bot]'
git -C "$KERNEL_DIR" config user.email 'github-actions[bot]@users.noreply.github.com'

fetch_with_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if git -C "$KERNEL_DIR" fetch "$@"; then
      return 0
    fi
    if [ "$attempt" -lt 5 ]; then
      echo "Fetch attempt $attempt/5 failed; retrying" >&2
      sleep "${FETCH_RETRY_DELAY_SECONDS:-5}"
    fi
  done
  echo "ERROR: git fetch failed after 5 attempts: $*" >&2
  return 1
}

fetch_with_retry --depth=256 "$STABLE_REPOSITORY" "$STABLE_COMMIT"
fetch_with_retry --deepen=256 "$BASE_REPO" "$BASE_COMMIT"

found=0
for round in 1 2 3 4 5 6 7 8; do
  if git -C "$KERNEL_DIR" merge-base "$BASE_COMMIT" "$STABLE_COMMIT" >/dev/null 2>&1; then
    found=1
    break
  fi
  echo "Deepening history for stable merge (round $round)"
  fetch_with_retry --deepen=256 "$BASE_REPO" "$BASE_COMMIT"
  fetch_with_retry --deepen=256 "$STABLE_REPOSITORY" "$STABLE_COMMIT"
done
if [ "$found" -ne 1 ]; then
  echo "ERROR: no merge base between GKI $BASE_COMMIT and stable $STABLE_COMMIT" >&2
  exit 1
fi

if ! git -C "$KERNEL_DIR" merge --no-edit --no-ff "$STABLE_COMMIT"; then
  git -C "$KERNEL_DIR" merge --abort || true
  echo "ERROR: stable merge conflict; not forcing the result" >&2
  exit 1
fi
MERGED_HEAD="$(git -C "$KERNEL_DIR" rev-parse HEAD)"
grep -Eq "^[[:space:]]*SUBLEVEL[[:space:]]*=[[:space:]]*$REQUIRED_SUBLEVEL([[:space:]]|$)" "$KERNEL_DIR/Makefile" || {
  echo "ERROR: expected SUBLEVEL=$REQUIRED_SUBLEVEL after stable merge" >&2
  exit 1
}

REPOSITORY_JSON="${STABLE_REPOSITORY//\\/\\\\}"
REPOSITORY_JSON="${REPOSITORY_JSON//\"/\\\"}"

mkdir -p "$PROVENANCE_DIR"
git -C "$KERNEL_DIR" diff --binary "$BASE_COMMIT" "$MERGED_HEAD" > "$PROVENANCE_DIR/stable-upgrade.patch"
{
  printf '{\n'
  printf '  "gki_base_commit": "%s",\n' "$BASE_COMMIT"
  printf '  "stable_commit": "%s",\n' "$STABLE_COMMIT"
  printf '  "merged_head": "%s",\n' "$MERGED_HEAD"
  printf '  "stable_repository": "%s",\n' "$REPOSITORY_JSON"
  printf '  "required_sublevel": "%s"\n' "$REQUIRED_SUBLEVEL"
  printf '}\n'
} > "$PROVENANCE_DIR/manifest.json"
printf 'KERNEL_SOURCE_COMMIT=%s\n' "$MERGED_HEAD" >> "$GITHUB_ENV"
printf 'GKI_BASE_COMMIT=%s\n' "$BASE_COMMIT" >> "$GITHUB_ENV"
printf 'STABLE_COMMIT=%s\n' "$STABLE_COMMIT" >> "$GITHUB_ENV"
printf 'MERGED_HEAD=%s\n' "$MERGED_HEAD" >> "$GITHUB_ENV"
printf 'commit=%s\n' "$STABLE_COMMIT" >> "$GITHUB_OUTPUT"
printf 'manifest=%s\n' "$PROVENANCE_DIR/manifest.json" >> "$GITHUB_OUTPUT"
printf 'Stable upgrade: GKI %s + stable %s -> %s (SUBLEVEL=%s)\n' \
  "$BASE_COMMIT" "$STABLE_COMMIT" "$MERGED_HEAD" "$REQUIRED_SUBLEVEL"
