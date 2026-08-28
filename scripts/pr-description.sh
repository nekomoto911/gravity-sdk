#!/usr/bin/env bash
# Extract the ## Description section from a GitHub PR body.
#
# Usage:
#   scripts/pr-description.sh <pr-number-or-url> [--repo OWNER/REPO]
#
# Prints the Description section (until the next ## heading), with HTML
# comments stripped. Intended as the squash-merge commit message body.
set -euo pipefail

usage() {
  echo "Usage: $0 <pr-number-or-url> [--repo OWNER/REPO]" >&2
  exit 2
}

if [[ $# -lt 1 ]]; then
  usage
fi

pr="$1"
shift
repo_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ $# -ge 2 ]] || usage
      repo_args=(--repo "$2")
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

# Accept bare numbers or full PR URLs.
if [[ "$pr" =~ ^https?://github.com/([^/]+/[^/]+)/pull/([0-9]+) ]]; then
  repo_args=(--repo "${BASH_REMATCH[1]}")
  pr="${BASH_REMATCH[2]}"
fi

body="$(gh pr view "$pr" "${repo_args[@]}" --json body -q .body)"

python3 - "$body" <<'PY'
import re, sys

body = sys.argv[1]
# Strip HTML comments.
body = re.sub(r"<!--.*?-->", "", body, flags=re.S)

# Find ## Description (allow optional trailing whitespace / BOM noise).
m = re.search(r"(?im)^##\s+Description\s*\n", body)
if not m:
    sys.stderr.write("error: no '## Description' section found in PR body\n")
    sys.exit(1)

rest = body[m.end():]
# Next markdown H2 ends the section.
nxt = re.search(r"(?m)^##\s+\S", rest)
section = rest[: nxt.start()] if nxt else rest
section = section.strip() + "\n"
if not section.strip():
    sys.stderr.write("error: '## Description' section is empty\n")
    sys.exit(1)
sys.stdout.write(section)
PY
