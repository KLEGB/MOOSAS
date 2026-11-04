#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mode="${1:-install}"
destination="${2:-.}"
[[ "$mode" == "install" || "$mode" == "stage" ]] || { echo 'Usage: build.sh [install|stage] [destination]'; exit 2; }
[[ "$mode" != "stage" || $# == 2 ]] || { echo 'stage requires an explicit destination'; exit 2; }
stage="$(mktemp -d .build-moosas-afn.XXXXXX)"
trap 'rm -f "$stage/MoosasAFN" "$stage/MoosasAFN.exe"; rmdir "$stage"' EXIT

for target in linux windows; do
  suffix=""
  [[ "$target" == windows ]] && suffix=".exe"
  GO111MODULE=off GOOS="$target" GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=0 \
    go build -trimpath -o "$stage/MoosasAFN$suffix" MoosasAFN.go
done
for target in linux windows; do
  suffix=""
  [[ "$target" == windows ]] && suffix=".exe"
  mkdir -p "$destination/$target-x86_64/bin"
  mv -f "$stage/MoosasAFN$suffix" "$destination/$target-x86_64/bin/MoosasAFN$suffix"
  chmod 755 "$destination/$target-x86_64/bin/MoosasAFN$suffix"
done
echo "Built linux/amd64 and windows/amd64 MoosasAFN binaries ($mode: $destination)."
