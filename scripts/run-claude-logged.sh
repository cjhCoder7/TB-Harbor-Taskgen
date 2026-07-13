#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"

if [[ "${TASKGEN_OPENAI_GATEWAY_ACTIVE:-}" != "1" && -f "${script_dir}/env_init.sh" ]]; then
  source "${script_dir}/env_init.sh"
fi

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m taskgen.claude.runner "$@"
