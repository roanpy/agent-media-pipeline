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
        --foreground|--dry-run|-h|--help) foreground=true ;;
    esac
done

if [[ "$1" =~ ^(search|profile|profiles|check|status|stop|cancel|doctor)$ ]]; then
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
import time

script, *args = sys.argv[1:]
log_path = f"/tmp/media-downloader-launch-{int(time.time())}-{os.getpid()}.log"
descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
os.chmod(log_path, 0o600)
with open(os.devnull, "rb") as stdin, os.fdopen(descriptor, "ab", buffering=0) as output:
    process = subprocess.Popen(
        [sys.executable, script, *args],
        stdin=stdin,
        stdout=output,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(script),
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
print(f"后台已启动，PID: {process.pid}")
print(f"启动日志: {log_path}")
PY
