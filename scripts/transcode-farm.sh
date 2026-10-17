#!/usr/bin/env bash
# Optional NVIDIA AV1 transcode example. Requires an AV1-capable GPU + ffmpeg.
set -euo pipefail
: "${LIBRARY:?Set LIBRARY to your source media directory}"
: "${OUT:?Set OUT to your destination directory}"
mkdir -p "$OUT"
while IFS= read -r -d '' file; do
  relative="$(realpath --relative-to="$LIBRARY" "$file")"
  destination="$OUT/${relative%.*}.av1.mkv"
  [[ -f "$destination" ]] && continue
  mkdir -p "$(dirname -- "$destination")"
  partial="$destination.partial"
  if ffmpeg -hide_banner -loglevel error -nostdin -i "$file" \
    -c:v av1_nvenc -preset p5 -cq 30 -b:v 0 -c:a copy -f matroska -y "$partial"; then
    mv -- "$partial" "$destination"
  else
    rm -f -- "$partial"
  fi
done < <(find "$LIBRARY" -type f \( -name '*.mkv' -o -name '*.mp4' \) -print0)
