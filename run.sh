#!/bin/bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SKILL_DIR/media-downloader.py"
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:${PATH:-}"

if [[ $# -eq 0 ]]; then
    exec /usr/bin/env python3 "$SCRIPT" --help
fi

foreground=false
for argument in "$@"; do
    case "$argument" in
        --foreground|--dry-run|--version|-h|--help) foreground=true ;;
    esac
done

if [[ "$1" =~ ^(search|sources|probe|add-source|profile|profiles|check|status|stop|cancel|doctor)$ ]]; then
    foreground=true
fi

args=()
for argument in "$@"; do
    [[ "$argument" == "--foreground" ]] || args+=("$argument")
done

if [[ "$foreground" == true || "${MEDIA_DOWNLOADER_FOREGROUND:-0}" == "1" ]]; then
    exec /usr/bin/env python3 "$SCRIPT" "${args[@]}"
fi

/usr/bin/env python3 - "$SCRIPT" "${args[@]}" <<'PY'
import os
import subprocess
import sys
import tempfile

script, *args = sys.argv[1:]
descriptor, log_path = tempfile.mkstemp(prefix="media-downloader-launch-", suffix=".log")
os.fchmod(descriptor, 0o600)
with open(os.devnull, "rb") as stdin, os.fdopen(descriptor, "ab", buffering=0) as output:
    process = subprocess.Popen(
        [sys.executable, script, *args],
        stdin=stdin,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
print(f"Started in background, PID: {process.pid}")
print(f"Launch log: {log_path}")
PY
