#!/usr/bin/env bash
# Files the four upstream PRs. Requires `gh` installed and authenticated
# (`gh auth login`). All four branches are already pushed to nekwo/hermes-agent.
set -euo pipefail

D="$(cd "$(dirname "$0")" && pwd)"
R=NousResearch/hermes-agent

gh pr create --repo "$R" --base main \
  --head nekwo:upstream-pr/windows-search-arg-pathconv \
  --title "fix(windows): stop rewriting program arguments into the MSYS /c/... form" \
  --body-file "$D/pr1-body.md"

gh pr create --repo "$R" --base main \
  --head nekwo:upstream-pr/windows-drive-letter-file-list \
  --title "fix(runner): stop --files/--paths from splitting Windows drive letters" \
  --body-file "$D/pr2-body.md"

gh pr create --repo "$R" --base main \
  --head nekwo:upstream-pr/reject-wsl-bash-stub \
  --title "fix(windows): never accept the System32 WSL stub as Git Bash" \
  --body-file "$D/pr4-body.md"

gh pr create --repo "$R" --base main \
  --head nekwo:upstream-pr/runner-retry-ownership \
  --title "fix(runner): don't spend the flake retry on a timeout" \
  --body-file "$D/pr5-body.md"
