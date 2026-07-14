#!/usr/bin/env bash
# Copy this file to scripts/github_init.sh and fill values locally.
# Never commit scripts/github_init.sh or real GitHub credentials.

# Recommended for authenticated SkillNet downloads and higher GitHub API limits.
export GITHUB_TOKEN=""

# Optional raw-content mirror. Leave unset unless the mirror is fully trusted.
# With the pinned SkillNet version, authenticated requests may send the GitHub
# Authorization header to the configured mirror host.
# export GITHUB_MIRROR=""
