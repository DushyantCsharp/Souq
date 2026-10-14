#!/usr/bin/env bash
# Download the newest non-expired eval report artifact produced by a run on this repository's main branch.
#
#   ci/fetch_eval_baseline.sh <artifact-name> <dest-dir>      (needs gh, GH_TOKEN with actions:read)
#
# Prints `run_id=<id>` and `sha=<head sha>` for $GITHUB_OUTPUT when found; exits 0 with no output when
# there is no baseline yet (first run, or every main artifact expired) so callers can say so.
# Only artifacts whose run's head repository is this repository count: a fork PR from its own branch
# named "main" uploads artifacts too, and must never become the baseline a PR is gated against.
set -euo pipefail

name=${1:?artifact name}
dest=${2:?destination dir}
repo=${GITHUB_REPOSITORY:?}

# Plain command substitutions so an API failure fails the step (set -e) instead of reading as "no baseline".
repo_id=$(gh api "repos/$repo" --jq .id)
found=$(gh api "repos/$repo/actions/artifacts?name=$name&per_page=100" --jq "
  [.artifacts[]
    | select(.expired | not)
    | select(.workflow_run.head_branch == \"main\")
    | select(.workflow_run.repository_id == $repo_id and .workflow_run.head_repository_id == $repo_id)]
  | sort_by(.created_at) | last
  | if . == null then \"\" else \"\\(.workflow_run.id) \\(.workflow_run.head_sha)\" end")

if [[ -z "$found" ]]; then
  echo "no '$name' artifact from main yet" >&2
  exit 0
fi
run_id=${found%% *}
sha=${found##* }
mkdir -p "$dest"
gh run download "$run_id" --repo "$repo" --name "$name" --dir "$dest"
echo "run_id=$run_id"
echo "sha=$sha"
