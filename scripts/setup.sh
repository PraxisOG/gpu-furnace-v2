#!/usr/bin/env bash
# Prepare private local configuration without starting a workload.
set -euo pipefail
umask 077
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/gpu-furnace"
mkdir -p "$config_dir"
if [[ ! -e "$config_dir/workloads.json" ]]; then
  cp "$repo_dir/heatd/workloads.example.json" "$config_dir/workloads.json"
fi
if [[ ! -e "$config_dir/furnace.env" ]]; then
  cp "$repo_dir/.env.example" "$config_dir/furnace.env"
  printf '\nFURNACE_CONFIG="%s/workloads.json"\n' "$config_dir" >> "$config_dir/furnace.env"
fi
python3 - "$repo_dir" "$config_dir/control.token" <<'PY'
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("heatd", Path(sys.argv[1]) / "heatd/heatd.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.load_control_token(sys.argv[2])
PY
printf 'Configuration ready: %s/workloads.json\nControl token: %s/control.token (kept private)\nNo workloads were started.\n' "$config_dir" "$config_dir"
