#!/usr/bin/env bash
# Squash-merge a PR using PR title as subject and ## Description as body.
#
# Usage:
#   scripts/pr-squash-merge.sh <pr-number-or-url> [--repo OWNER/REPO] [extra gh pr merge args...]
#
# Examples:
#   scripts/pr-squash-merge.sh 824
#   scripts/pr-squash-merge.sh 824 --repo Galxe/gravity-sdk --admin
set -euo pipefail

usage() {
  echo "Usage: $0 <pr-number-or-url> [--repo OWNER/REPO] [extra gh pr merge args...]" >&2
  exit 2
}

if [[ $# -lt 1 ]]; then
  usage
fi

pr="$1"
shift

repo_args=()
extra=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ $# -ge 2 ]] || usage
      repo_args=(--repo "$2")
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      extra+=("$1")
      shift
      ;;
  esac
done

if [[ "$pr" =~ ^https?://github.com/([^/]+/[^/]+)/pull/([0-9]+) ]]; then
  repo_args=(--repo "${BASH_REMATCH[1]}")
  pr="${BASH_REMATCH[2]}"
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
desc="$("$root/scripts/pr-description.sh" "$pr" "${repo_args[@]}")"
title="$(gh pr view "$pr" "${repo_args[@]}" --json title -q .title)"

echo "Squash-merging #$pr"
echo "---- subject ----"
echo "$title"
echo "---- body (## Description) ----"
printf '%s' "$desc"
echo "-----------------"

gh pr merge "$pr" "${repo_args[@]}" --squash --subject "$title" --body "$desc" "${extra[@]}"
