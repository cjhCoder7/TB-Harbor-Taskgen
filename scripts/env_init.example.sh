#!/usr/bin/env bash
# Copy this file to scripts/env_init.sh and fill values locally.
# Never commit scripts/env_init.sh or real provider credentials.

export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://openrouter.ai/api}"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$OPENROUTER_API_KEY}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
