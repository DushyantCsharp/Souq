#!/usr/bin/env bash
# Create or update the single PR comment that starts with <marker>, so re-runs edit in place instead of
# stacking comments.
#
#   ci/upsert_pr_comment.sh <pr-number> <body-file> [marker]   (needs gh, GH_TOKEN with pull-requests:write)
set -euo pipefail

pr=${1:?pr number}
body=${2:?body file}
marker=${3:-<!-- souq-eval-report -->}
repo=${GITHUB_REPOSITORY:?}

existing=$(gh api --paginate "repos/$repo/issues/$pr/comments" \
  --jq ".[] | select(.user.type == \"Bot\" and (.body | startswith(\"$marker\"))) | .id" | head -n1)

if [[ -n "$existing" ]]; then
  gh api --method PATCH "repos/$repo/issues/comments/$existing" -F "body=@$body" --jq .html_url
else
  gh api --method POST "repos/$repo/issues/$pr/comments" -F "body=@$body" --jq .html_url
fi
