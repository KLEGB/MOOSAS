#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mode="${1:-install}"
destination="${2:-.}"
[[ "$mode" == "install" || "$mode" == "stage" ]] || { echo 'Usage: build.sh [install|stage] [destination]'; exit 2; }
[[ "$mode" != "stage" || $# == 2 ]] || { echo 'stage requires an explicit destination'; exit 2; }
mkdir -p "$destination"
stage="$(mktemp -d .build-moorad.XXXXXX)"
trap 'rm -f "$stage/MoosasRad" "$stage/MoosasRad.exe"; rmdir "$stage"' EXIT

echo "Building MoosasRad with: $(go version)"
for target in linux windows; do
  suffix=""
  [[ "$target" == windows ]] && suffix=".exe"
  GO111MODULE=on GOOS="$target" GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=0 \
    go build -trimpath -o "$stage/MoosasRad$suffix" MoosasRad.go MoosasBVH.go
done

test -s "$stage/MoosasRad"
test -s "$stage/MoosasRad.exe"
for program in MoosasRad MoosasRad.exe; do
  go version -m "$stage/$program"
done
for target in linux windows; do
  suffix=""
  [[ "$target" == windows ]] && suffix=".exe"
  mkdir -p "$destination/$target-x86_64/bin"
  mv -f "$stage/MoosasRad$suffix" "$destination/$target-x86_64/bin/MoosasRad$suffix"
  chmod 755 "$destination/$target-x86_64/bin/MoosasRad$suffix"
done
echo "Built linux/amd64 and windows/amd64 MoosasRad binaries ($mode: $destination)."
