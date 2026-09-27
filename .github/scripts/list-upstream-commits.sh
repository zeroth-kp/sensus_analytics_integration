#!/usr/bin/env bash
# Write a Markdown checklist of upstream commits not yet reviewed and print how many there are.
#
# Usage: list-upstream-commits.sh <body-file>
# Expects an `upstream` remote whose main branch has been fetched, and a
# `.upstream-reviewed` file holding the SHA of the newest upstream commit
# already reviewed.
set -euo pipefail

# Read lines from stdin into the named array (portable to bash 3.2; no mapfile).
read_lines() {
  local line
  eval "$1=()"
  while IFS= read -r line; do
    [ -n "$line" ] && eval "$1+=(\"\$line\")"
  done
}

body_file="$1"
upstream_repo="zestysoft/sensus_analytics_integration"
max_files=6
fallback_count=50

reviewed="$(tr -d '[:space:]' < .upstream-reviewed)"
warning=""
if git cat-file -e "${reviewed}^{commit}" 2>/dev/null && git merge-base --is-ancestor "$reviewed" upstream/main; then
  read_lines commits < <(git rev-list --reverse "${reviewed}..upstream/main")
else
  # Upstream rewrote its history (or the file holds a bad SHA): list recent
  # commits instead of failing silently.
  warning="> [!WARNING]
> The last reviewed commit \`${reviewed}\` is not part of upstream's \`main\` history, so upstream may have rewritten it. The ${fallback_count} most recent upstream commits are listed below instead; review them and reset \`.upstream-reviewed\`.
"
  read_lines commits < <(git rev-list --reverse --max-count="$fallback_count" upstream/main)
fi

count="${#commits[@]}"
[ "$count" -gt 0 ] || commits=()
{
  if [ -n "$warning" ]; then
    printf '%s\n' "$warning"
  fi
  echo "New commits on [${upstream_repo}](https://github.com/${upstream_repo}) since \`${reviewed:0:7}\`, oldest first:"
  echo
  for sha in ${commits[@]+"${commits[@]}"}; do
    subject="$(git log -1 --format=%s "$sha")"
    read_lines files < <(git show --name-only --format= "$sha")
    listed="$(printf '%s\n' "${files[@]:0:$max_files}" | sed 's/^/`/; s/$/`/' | paste -sd ',' - | sed 's/,/, /g')"
    if [ "${#files[@]}" -gt "$max_files" ]; then
      listed="${listed}, +$(( ${#files[@]} - max_files )) more"
    fi
    echo "- [ ] [\`${sha:0:7}\`](https://github.com/${upstream_repo}/commit/${sha}) ${subject} — ${listed:-no files}"
  done
  cat <<'FOOTER'

**Triage each commit:**
- Sensus API behavior (login, errors, outages, response shapes, endpoints): port by default.
- Statistics import logic (DST, running sums, Energy): compare against this fork's importer and port the fix, not the code.
- New sensors or features: port only if wanted.
- Restructuring, CI, docs, releases, dependency bumps: skip.

Ported changes go in their own PRs with a `Ported-from: zestysoft/sensus_analytics_integration@<sha>` trailer. When every commit is triaged, set `.upstream-reviewed` to the newest triaged SHA and close this issue with a short summary. This list is regenerated weekly from `.upstream-reviewed`, so checkboxes are working notes only.
FOOTER
} > "$body_file"

echo "$count"
