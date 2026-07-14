#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${script_dir}/env_init.sh" ]]; then
  source "${script_dir}/env_init.sh"
fi

uv tool install harbor==0.13.2
uv tool install --upgrade skillnet-ai==0.1.0
uv tool install 'litellm[proxy]==1.91.1'
