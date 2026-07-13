#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

use_openai=false
for argument in "$@"; do
  if [[ "${argument}" == "--openai" ]]; then
    use_openai=true
    break
  fi
done

if [[ "${use_openai}" == true ]]; then
  environment_file="${script_dir}/env_openai_init.sh"
else
  environment_file="${script_dir}/env_init.sh"
fi

if [[ -f "${environment_file}" ]]; then
  source "${environment_file}"
fi

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m taskgen.cli "$@"
