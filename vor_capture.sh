#!/usr/bin/env bash
set -euo pipefail

. /opt/rb-vor/bin/activate

while sleep 1;do
for FREQ in "114.4" "109.4" "111.8" "114.7" "115.2"; do

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


    python3 tools/vor_capture.py -t 3  --ppm 2 --gain 14.4 --site "${FREQ}_${GETFILENAME}" --gps-track  "$GPSTrueCourse"  --lat "$GPSLatitude" --lon "$GPSLongitude"  --backend rtl_sdr -f "${FREQ}"
    
done
done