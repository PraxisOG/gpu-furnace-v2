#!/usr/bin/env bash
# Optional container bench. Use a PyTorch image compatible with your GPU driver.
set -euo pipefail
: "${FURNACE_IMAGE:?Set FURNACE_IMAGE to your compatible PyTorch container image}"
backend="${FURNACE_GPU_BACKEND:?Set FURNACE_GPU_BACKEND to amd or nvidia}"
container="${FURNACE_CONTAINER:-furnace-bench}"
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
args=(--rm --name "$container" --entrypoint python3)
case "$backend" in
  amd)
    args+=(--device /dev/kfd --device /dev/dri)
    # Resolve device groups on this machine instead of hard-coding numeric IDs.
    for device in /dev/kfd /dev/dri/renderD*; do
      [[ -e "$device" ]] && args+=(--group-add "$(stat -c '%g' "$device")")
    done
    ;;
  nvidia) args+=(--gpus all) ;;
  *) printf 'Unsupported GPU backend: %s\n' "$backend" >&2; exit 2 ;;
esac
exec docker run "${args[@]}" \
  -e "HEATER_SIZE=${HEATER_SIZE:-4096}" \
  -v "$repo_dir/scripts/heater.py:/furnace/heater.py:ro" \
  "$FURNACE_IMAGE" -u /furnace/heater.py
