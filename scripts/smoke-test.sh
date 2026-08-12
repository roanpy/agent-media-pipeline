#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d /tmp/media-downloader-smoke.XXXXXX)"
trap 'rm -rf "$TEST_ROOT" /tmp/media-downloader-Smoke\ Test.log /tmp/media-downloader-Missing\ Target.log' EXIT

cd "$PROJECT_DIR"
python3 -m py_compile media-downloader.py
bash -n run.sh scripts/download.sh
python3 -m json.tool config.json >/dev/null
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null

mkdir -p "$TEST_ROOT/base/Smoke Test/Season 1" "$TEST_ROOT/target"
ffmpeg -hide_banner -loglevel error \
    -f lavfi -i testsrc2=size=320x240:rate=24 \
    -f lavfi -i sine=frequency=1000 \
    -t 121 -c:v libx264 -preset ultrafast -c:a aac \
    "$TEST_ROOT/base/Smoke Test/Season 1/Smoke Test - S01E01.mp4"

env \
    MEDIA_DOWNLOADER_BASE_DIR="$TEST_ROOT/base" \
    MEDIA_DOWNLOADER_STATE_DIR="$TEST_ROOT/state" \
    MEDIA_DOWNLOADER_TARGET_DIR="$TEST_ROOT/target" \
    MEDIA_DOWNLOADER_STATUS_FILE="$TEST_ROOT/status.json" \
    MEDIA_DOWNLOADER_STABLE_CHECK_SECONDS=1 \
    python3 media-downloader.py "Smoke Test"

test ! -e "$TEST_ROOT/base/Smoke Test/Season 1/Smoke Test - S01E01.mp4"
test -e "$TEST_ROOT/target/Smoke Test/Season 1/Smoke Test - S01E01.mp4"

mkdir -p "$TEST_ROOT/base/Missing Target/Season 1"
cp "$TEST_ROOT/target/Smoke Test/Season 1/Smoke Test - S01E01.mp4" \
    "$TEST_ROOT/base/Missing Target/Season 1/Missing Target - S01E01.mp4"

if env \
    MEDIA_DOWNLOADER_BASE_DIR="$TEST_ROOT/base" \
    MEDIA_DOWNLOADER_STATE_DIR="$TEST_ROOT/state" \
    MEDIA_DOWNLOADER_TARGET_DIR="$TEST_ROOT/not-mounted" \
    MEDIA_DOWNLOADER_STATUS_FILE="$TEST_ROOT/status.json" \
    MEDIA_DOWNLOADER_STABLE_CHECK_SECONDS=1 \
    python3 media-downloader.py "Missing Target" >/dev/null 2>&1; then
    echo "missing target unexpectedly succeeded" >&2
    exit 1
fi

test -e "$TEST_ROOT/base/Missing Target/Season 1/Missing Target - S01E01.mp4"
test ! -e "$TEST_ROOT/not-mounted"
echo "smoke test passed"
