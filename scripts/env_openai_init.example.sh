#!/usr/bin/env bash
# Copy this file to scripts/env_openai_init.sh and fill values locally.
# Never commit scripts/env_openai_init.sh or real provider credentials.

# The current LiteLLM OpenAI-provider route requires POST /v1/responses.
# The same API base may also expose /v1/chat/completions.
# OPENAI_BASE_URL should be the API base URL ending in /v1.
# Available model names depend on the configured API provider.

export OPENAI_BASE_URL=""
export OPENAI_API_KEY=""
