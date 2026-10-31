#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://localhost/getSituation}"

if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required." >&2
  exit 1
fi

json="$(curl -fsS "$URL")"

GPSTime="$(jq -r '.GPSTime // empty' <<< "$json")"
GPSLatitude="$(jq -r '.GPSLatitude // empty' <<< "$json")"
GPSLongitude="$(jq -r '.GPSLongitude // empty' <<< "$json")"
GPSAltitudeMSL="$(jq -r '.GPSAltitudeMSL // empty' <<< "$json")"
GPSTrueCourse="$(jq -r '.GPSTrueCourse // empty' <<< "$json")"
EPOCH=$(date +%s)

GETFILENAME="${GPSTime}_${GPSLatitude}_${GPSLongitude}_${GPSAltitudeMSL}_${GPSTrueCourse}_${EPOCH}"

export GPSTime GPSLatitude GPSLongitude GPSAltitudeMSL GPSTrueCourse GETFILENAME

echo "$GETFILENAME"
