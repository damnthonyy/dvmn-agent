#!/usr/bin/env bash
# scripts/sync-with-upstream.sh
#
# Local equivalent of .github/workflows/sync-upstream.yml: merge upstream/main
# into a fresh branch off `prod` and open a PR. CI runs this automatically at
# 02:00 UTC; use this script for an out-of-cycle sync.
#
# It never pushes to `prod` or `main` directly — all changes land on a
# `sync/upstream-*` branch and go through a PR.

set -euo pipefail

UPSTREAM_URL="https://github.com/qodo-ai/pr-agent.git"
BASE_BRANCH="prod"
SYNC_BRANCH="sync/upstream-$(date +%Y%m%d%H%M%S)"

echo "📦 Syncing with upstream (${UPSTREAM_URL})..."

git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM_URL"
git fetch upstream main --tags
git fetch origin "$BASE_BRANCH"

git checkout -b "$SYNC_BRANCH" "origin/${BASE_BRANCH}"

git merge --no-commit --no-edit upstream/main || true

# The fork owns its CI — keep .github/workflows/ exactly as on the base branch
# (mirrors the CI workflow; a GITHUB_TOKEN push is refused if it touches
# .github/workflows/**).
rm -rf .github/workflows
git checkout "origin/${BASE_BRANCH}" -- .github/workflows

if [ -n "$(git ls-files --unmerged)" ]; then
  echo "⚠️  Merge conflicts — committing them as-is so they can be resolved in the PR."
  commit_msg="sync: merge upstream/main (UNRESOLVED CONFLICTS — resolve in this PR)"
else
  echo "✅ Clean merge."
  commit_msg="sync: merge upstream/main"
fi

git add -A

if git diff --cached --quiet; then
  echo "Nothing to sync — ${BASE_BRANCH} already matches upstream/main (workflow-only changes are skipped by design)."
  git merge --abort 2>/dev/null || true
  git checkout -
  git branch -D "$SYNC_BRANCH"
  exit 0
fi

git commit --no-verify -m "$commit_msg"

git push origin "$SYNC_BRANCH"

if command -v gh >/dev/null 2>&1; then
  gh pr create --base "$BASE_BRANCH" --head "$SYNC_BRANCH" \
    --title "sync: bring changes from upstream/main" \
    --body "Merge of upstream/main into a branch off ${BASE_BRANCH}. Resolve any committed conflicts before merging."
else
  echo "Branch pushed. Open a PR from ${SYNC_BRANCH} into ${BASE_BRANCH}."
fi

echo "✅ Done."
