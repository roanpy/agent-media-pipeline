#!/bin/bash
set -e
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$SKILL_DIR/.runtime"
STATUS_FILE="$RUNTIME_DIR/media-downloader-status.json"
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"
mkdir -p "$RUNTIME_DIR"

validate_show_name() {
    local show_name="$1"
    local profile="${2:-}"
    if [[ -z "$show_name" ]]; then
        echo "错误: 请提供剧名"
        exit 1
    fi
    if [[ "$profile" != movie* && "$show_name" =~ [Mm]ovie|[Mm]ovies|电影 ]]; then
        echo "错误: 当前入口默认处理电视剧/剧集；电影需通过 --profile=movie720 或 --profile=movie1080 明确指定"
        exit 1
    fi
}

extract_profile() {
    PROFILE=""
    CLEAN_ARGS=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --profile=*)
                PROFILE="${1#--profile=}"
                shift
                ;;
            --profile)
                if [[ -z "${2:-}" ]]; then
                    echo "错误: --profile 需要预设名" >&2
                    exit 1
                fi
                PROFILE="$2"
                shift 2
                ;;
            *)
                CLEAN_ARGS+=("$1")
                shift
                ;;
        esac
    done
}

launch_detached() {
    local show_name="$1"
    local magnet_link="$2"
    local log_file="/tmp/media-downloader-${show_name}.log"

    /usr/bin/env python3 - "$SKILL_DIR/media-downloader.py" "$show_name" "$magnet_link" "$log_file" <<'PY'
import os
import subprocess
import sys

script_path, show_name, magnet_link, log_file = sys.argv[1:5]
env = os.environ.copy()
with open(os.devnull, "rb") as devnull, open(log_file, "ab", buffering=0) as log_handle:
    proc = subprocess.Popen(
        [sys.executable, script_path, show_name, magnet_link],
        stdin=devnull,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(script_path),
        start_new_session=True,
        close_fds=True,
        env=env,
    )
print(proc.pid)
PY
}

launch_adopt() {
    local show_name="$1"
    local adopt_source="$2"
    local log_file="/tmp/media-downloader-${show_name}.log"

    /usr/bin/env python3 - "$SKILL_DIR/media-downloader.py" "$show_name" "" "$log_file" "$adopt_source" <<'PY'
import os
import subprocess
import sys

script_path, show_name, _, log_file, adopt_source = sys.argv[1:6]
env = os.environ.copy()
env["START_MODE"] = "adopt"
env["ADOPT_SOURCE"] = adopt_source
with open(os.devnull, "rb") as devnull, open(log_file, "ab", buffering=0) as log_handle:
    proc = subprocess.Popen(
        [sys.executable, script_path, show_name],
        stdin=devnull,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(script_path),
        start_new_session=True,
        close_fds=True,
        env=env,
    )
print(proc.pid)
PY
}

usage() {
    echo "Usage: $0 {process|check|stop|resume|adopt|profile|download|organize|status|cancel} [args...]"
    echo "  process [剧名] [--profile=预设]     中性入口：处理本地已有媒体"
    echo "  check [剧名]                        中性入口：查看任务状态"
    echo "  stop [剧名]                         中性入口：安全停止任务"
    echo "  resume [剧名] [磁力] [--profile=预设] 中性入口：下载或恢复任务"
    echo "  adopt [剧名] [来源目录] [--profile=预设] 手动接管卡住的下载"
    echo "  profile [预设名]                     列出/设置转码预设（tv720/tv1080/movie720/movie1080）"
    echo "  download [剧名] [磁力] [--profile=预设] 下载+转换+转移到目标磁盘"
    echo "  organize [剧名] [--profile=预设]    校验本地输出并转移到目标磁盘"
    echo "  status [剧名]                       查看状态"
    echo "  cancel [剧名]                       安全停止任务"
    exit 1
}

case "$1" in
    download|resume)
        shift
        extract_profile "$@"
        set -- "${CLEAN_ARGS[@]}"
        if [[ -z "$1" || -z "$2" ]]; then echo "错误: 请提供剧名和磁力"; exit 1; fi
        validate_show_name "$1" "$PROFILE"
        echo "[$(date '+%H:%M:%S')] 开始下载: $1 ${PROFILE:+(预设=$PROFILE)}"
        export LC_ALL=en_US.UTF-8
        export LANG=en_US.UTF-8
        export MEDIA_DOWNLOADER_PROFILE="$PROFILE"
        PID="$(launch_detached "$1" "$2")"
        echo "后台已启动，PID: $PID"
        ;;
    organize|process)
        shift
        extract_profile "$@"
        set -- "${CLEAN_ARGS[@]}"
        if [[ -z "$1" ]]; then echo "错误: 请提供剧名"; exit 1; fi
        validate_show_name "$1" "$PROFILE"
        echo "[$(date '+%H:%M:%S')] 开始整理: $1 ${PROFILE:+(预设=$PROFILE)}"
        export LC_ALL=en_US.UTF-8
        export LANG=en_US.UTF-8
        export MEDIA_DOWNLOADER_PROFILE="$PROFILE"
        PID="$(launch_detached "$1" "")"
        echo "后台已启动，PID: $PID"
        ;;
    adopt)
        shift
        extract_profile "$@"
        set -- "${CLEAN_ARGS[@]}"
        if [[ -z "$1" ]]; then echo "错误: 请提供剧名"; exit 1; fi
        validate_show_name "$1" "$PROFILE"
        ADOPT_SRC="${2:-}"
        echo "[$(date '+%H:%M:%S')] 手动接管: $1 ${ADOPT_SRC:+来源=$ADOPT_SRC} ${PROFILE:+(预设=$PROFILE)}"
        export LC_ALL=en_US.UTF-8
        export LANG=en_US.UTF-8
        export MEDIA_DOWNLOADER_PROFILE="$PROFILE"
        PID="$(launch_adopt "$1" "$ADOPT_SRC")"
        echo "后台已启动，PID: $PID"
        ;;
    profile)
        /usr/bin/env python3 - "$SKILL_DIR/config.json" "${2:-}" <<'PY'
import json, sys
config_path = sys.argv[1]
set_profile = sys.argv[2] if len(sys.argv) > 2 else ""
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)
profiles = config.get("profiles", {})
default = config.get("defaultProfile", "legacy")
if not set_profile:
    print(f"当前默认预设: {default}")
    print()
    print("可用预设:")
    legacy_marker = " ← 默认" if default == "legacy" else ""
    print(f"  legacy: 当前兼容默认 | tv | 720p | 500k | 64k | /tmp/agent-media-library/TV{legacy_marker}")
    for name, p in profiles.items():
        marker = " ← 默认" if name == default else ""
        print(f"  {name}: {p.get('label', name)} | {p.get('type','tv')} | {p.get('resolution','?')}p | {p.get('videoBitrate','?')} | {p.get('audioBitrate','?')} | {p.get('targetDir') or p.get('nasRoot','')}{marker}")
    print()
    print("用法: run.sh profile <预设名>        设置默认预设")
    print("      run.sh resume 剧名 磁力 --profile=预设名  指定本次预设")
else:
    if set_profile != "legacy" and set_profile not in profiles:
        print(f"错误: 预设 '{set_profile}' 不存在")
        print(f"可用: legacy, {', '.join(profiles.keys())}")
        sys.exit(1)
    config["defaultProfile"] = set_profile
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    import os
    os.replace(tmp, config_path)
    if set_profile == "legacy":
        print("默认预设已设置为: legacy (当前兼容默认)")
        print("  类型=tv 分辨率=720p 视频=500k 音频=64k")
        print("  目标磁盘=/tmp/agent-media-library/TV")
    else:
        p = profiles[set_profile]
        print(f"默认预设已设置为: {set_profile} ({p.get('label', set_profile)})")
        print(f"  类型={p.get('type','tv')} 分辨率={p.get('resolution','?')}p 视频={p.get('videoBitrate','?')} 音频={p.get('audioBitrate','?')}")
        print(f"  目标磁盘={p.get('targetDir') or p.get('nasRoot','')}")
PY
        ;;
    status|check)
        SHOW_NAME="$2"
        if [[ -f "$STATUS_FILE" ]]; then
            /usr/bin/env python3 -c '
import json, sys
from pathlib import Path
VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "m2ts", "vob", "rm", "rmvb", "3gp"}
path = Path(sys.argv[1])
show_filter = sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8"))
if not data:
    print("暂无状态记录")
    raise SystemExit(0)

def count_partial_files(download_dir):
    root = Path(download_dir)
    if not download_dir or not root.exists():
        return 0
    total = 0
    for item in root.rglob("*"):
        if not item.is_file() or not item.name.endswith(".part"):
            continue
        original_name = item.name[:-5]
        if Path(original_name).suffix.lstrip(".").lower() in VIDEO_EXTS:
            total += 1
    return total

matched = False
for show, state in data.items():
    if show_filter and show != show_filter:
        continue
    matched = True
    counts = state.get("counts", {})
    plex_refresh = state.get("plex_refresh") or {}
    current = state.get("current_file") or "-"
    op = state.get("current_operation") or state.get("phase") or "unknown"
    retry_waiting = counts.get("retry_waiting", 0)
    phase = state.get("phase", "-")
    updated = state.get("updated_at", "-")
    last_error = state.get("last_error")
    source = counts.get("source_videos", 0)
    partial = counts.get("partial_videos")
    if partial is None:
        partial = count_partial_files(state.get("download_dir", ""))
    ready = counts.get("stable_ready", 0)
    pending = counts.get("pending_transcodes", 0)
    local = counts.get("local_mp4", 0)
    nas = counts.get("nas_mp4", 0)
    failed = counts.get("failed", 0)
    plex_status = plex_refresh.get("status") or "-"
    plex_mode = plex_refresh.get("mode") or "-"
    plex_detail = plex_refresh.get("detail") or "-"
    profile = state.get("active_profile", "-")
    media_type = state.get("media_type", "-")
    print(f"[{show}] phase={phase} profile={profile} type={media_type} updated={updated}")
    print(f"  op={op} file={current}")
    print(f"  source={source} partial={partial} ready={ready} pending={pending} local={local} nas={nas} retry_waiting={retry_waiting} failed={failed}")
    print(f"  plex_refresh={plex_status} mode={plex_mode} detail={plex_detail}")
    if last_error:
        print(f"  last_error={last_error}")
if show_filter and not matched:
    print(f"未找到任务: {show_filter}")
' "$STATUS_FILE" "$SHOW_NAME"
        else
            echo "暂无状态文件"
        fi
        echo "=== 运行中任务 ==="
        if [[ -n "$SHOW_NAME" ]]; then
            ps aux | grep "media-downloader.py" | grep -F "$SHOW_NAME" | grep -v grep | awk '{print $2, $(NF-1), $NF}'
        else
            ps aux | grep "media-downloader.py" | grep -v grep | awk '{print $2, $(NF-1), $NF}'
        fi
        ;;
    cancel|stop)
        SHOW_NAME="$2"
        if [[ -n "$SHOW_NAME" ]]; then
            pkill -TERM -f "media-downloader.py.*$SHOW_NAME" 2>/dev/null && echo "已发送停止信号: $SHOW_NAME" || echo "未找到运行中任务"
        else
            echo "错误: 请提供剧名"
            exit 1
        fi
        ;;
    *)
        usage
        ;;
esac
