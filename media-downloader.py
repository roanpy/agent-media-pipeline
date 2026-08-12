#!/usr/bin/env python3
"""
media-downloader 稳定版流水线
usage: media-downloader.py [剧名] [磁力链接]
       media-downloader.py [剧名]
"""
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

RAW_ARGS = sys.argv[1:]
if RAW_ARGS[:1] and RAW_ARGS[0] in {"download", "organize", "adopt"}:
    RAW_ARGS = RAW_ARGS[1:]

SHOW_NAME = RAW_ARGS[0] if len(RAW_ARGS) > 0 else ""
MAGNET = RAW_ARGS[1] if len(RAW_ARGS) > 1 else ""
ADOPT_SOURCE = os.environ.get("ADOPT_SOURCE", "")
ACTIVE_PROFILE_NAME = os.environ.get("MEDIA_DOWNLOADER_PROFILE", "")
MEDIA_TYPE = "tv"
ACTIVE_PROFILE = {}
if ADOPT_SOURCE and not MAGNET:
    START_MODE = "adopt"
elif MAGNET:
    START_MODE = "download"
else:
    START_MODE = "organize"

DEFAULT_BASE_DIR = Path("/tmp/agent-media-pipeline-work")
DEFAULT_NAS_ROOT = Path("/tmp/agent-media-library/TV")
DEFAULT_SEASON_NAME = "Season 1"
SKILL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SKILL_DIR / ".runtime"
SKILL_CONFIG_FILE = SKILL_DIR / "config.json"
DEFAULT_RUNTIME_STATE_ROOT = DEFAULT_BASE_DIR / ".media-downloader-state"
DEFAULT_TRANSMISSION_UPLOAD_LIMIT_KBPS = 500
DEFAULT_SEED_IDLE_SECONDS = 0
DEFAULT_TIMEOUT_HOURS = 24
DEFAULT_STABLE_CHECK_SECONDS = 5
DEFAULT_STABLE_POLLS = 3
DEFAULT_DOWNLOAD_SETTLE_POLLS = 5
DEFAULT_MIN_SOURCE_VIDEO_BYTES = 1 * 1024 * 1024
DEFAULT_NAS_AUTO_MOUNT = True

BASE_DIR = DEFAULT_BASE_DIR
SEASON_NAME = DEFAULT_SEASON_NAME
TMP_DIR = BASE_DIR / f"tmp_{SHOW_NAME}"
FINAL_DIR = BASE_DIR / SHOW_NAME / SEASON_NAME
NAS_ROOT = DEFAULT_NAS_ROOT
CANONICAL_SHOW_NAME = SHOW_NAME
NAS_SHOW_DIR = NAS_ROOT / CANONICAL_SHOW_NAME / SEASON_NAME
RUNTIME_STATE_ROOT = DEFAULT_RUNTIME_STATE_ROOT
SHOW_STATE_DIR = RUNTIME_STATE_ROOT / SHOW_NAME
TRANSMISSION_CONFIG_DIR = SHOW_STATE_DIR / "transmission"
TRANSMISSION_UPLOAD_LIMIT_KBPS = DEFAULT_TRANSMISSION_UPLOAD_LIMIT_KBPS
SEED_IDLE_SECONDS = DEFAULT_SEED_IDLE_SECONDS
STATUS_FILE = Path(os.environ.get("MEDIA_DOWNLOADER_STATUS_FILE", RUNTIME_DIR / "media-downloader-status.json"))
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
SKILL_CONFIG = {}
NAS_AUTO_MOUNT = DEFAULT_NAS_AUTO_MOUNT
NAS_MOUNT_URL = ""
LOG_FILE = Path(f"/tmp/media-downloader-{SHOW_NAME}.log")
LOCK_FILE = Path(f"/tmp/media-downloader-{SHOW_NAME}.lock")

VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "m2ts", "vob", "rm", "rmvb", "3gp"}
POLL_INTERVAL = 20
STABLE_CHECK_SECONDS = DEFAULT_STABLE_CHECK_SECONDS
STABLE_POLLS = DEFAULT_STABLE_POLLS
DOWNLOAD_SETTLE_POLLS = DEFAULT_DOWNLOAD_SETTLE_POLLS
TIMEOUT_HOURS = DEFAULT_TIMEOUT_HOURS
STALE_TASK_HOURS = 48
STALE_RESUME_PHASES = {"downloading", "timeout", "stopped"}
MIN_SOURCE_VIDEO_BYTES = DEFAULT_MIN_SOURCE_VIDEO_BYTES
MIN_EPISODE_DURATION_SECONDS = 120
COPY_BUFFER_SIZE = 8 * 1024 * 1024
MAX_TRANSCODE_ATTEMPTS = 3
RETRY_DELAYS = [120, 300, 900]
EPISODE_PATTERNS = [
    re.compile(r"[Ss](?P<season>\d{1,2})[ ._-]?[Ee](?P<episode>\d{1,3})"),
    re.compile(r"(?:^|[ ._\-\[])[Ee][Pp]?(?P<episode>\d{1,3})(?:$|[ ._\-\]])"),
    re.compile(r"第\s*(?P<episode>\d{1,3})\s*集"),
]
COMMAND_ENV = {
    **os.environ,
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
    "LC_ALL": "en_US.UTF-8",
    "LANG": "en_US.UTF-8",
}
TVMAZE_IMAGE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "media-downloader/1.0",
}

STOP_REQUESTED = False
ACTIVE_PROCESS = None
ACTIVE_OPERATION = "idle"
ACTIVE_TARGET = ""
RUN_LOCK_HANDLE = None
STATUS_LOCK_FILE = RUNTIME_DIR / "media-downloader-status.lock"

DEFAULT_PROFILE = {
    "type": "tv",
    "label": "Default",
    "resolution": 720,
    "videoBitrate": "500k",
    "audioBitrate": "64k",
    "videoCodec": "libx264",
    "preset": "fast",
    "nasRoot": str(DEFAULT_NAS_ROOT),
}


def load_profile():
    global ACTIVE_PROFILE, ACTIVE_PROFILE_NAME, MEDIA_TYPE
    config = read_json_file(SKILL_CONFIG_FILE)
    profiles = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        ACTIVE_PROFILE = dict(DEFAULT_PROFILE)
        ACTIVE_PROFILE_NAME = "default"
        MEDIA_TYPE = "tv"
        return
    name = ACTIVE_PROFILE_NAME or config.get("defaultProfile") or "legacy"
    if name == "legacy":
        ACTIVE_PROFILE = dict(DEFAULT_PROFILE)
        ACTIVE_PROFILE_NAME = "legacy"
        MEDIA_TYPE = "tv"
        return
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        log(f"预设 '{name}' 不存在，使用默认配置")
        ACTIVE_PROFILE = dict(DEFAULT_PROFILE)
        ACTIVE_PROFILE_NAME = "legacy"
        MEDIA_TYPE = "tv"
        return
    ACTIVE_PROFILE = profile
    ACTIVE_PROFILE_NAME = name
    MEDIA_TYPE = profile.get("type", "tv")
    log(f"使用预设: {name} ({profile.get('label', name)}) type={MEDIA_TYPE}")


@contextmanager
def status_file_lock():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = open(STATUS_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_handle.close()


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(line + "\n")


def notify(message):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "Media Downloader"'],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def load_status_map_unlocked():
    if not STATUS_FILE.exists():
        return {}
    try:
        with open(STATUS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_status_map():
    with status_file_lock():
        return load_status_map_unlocked()


def save_status_map(all_state):
    with status_file_lock():
        atomic_write_json(STATUS_FILE, all_state)


def mutate_status_map(mutator):
    with status_file_lock():
        all_state = load_status_map_unlocked()
        changed = mutator(all_state)
        if changed:
            atomic_write_json(STATUS_FILE, all_state)
        return changed


def parse_status_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def download_meta_path(download_dir=None):
    root = Path(download_dir) if download_dir else TMP_DIR
    return root / ".download-meta.json"


def read_download_meta(download_dir=None):
    path = download_meta_path(download_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_download_meta():
    if START_MODE != "download" or not MAGNET:
        return
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(download_meta_path(), {
        "show": SHOW_NAME,
        "magnet": MAGNET,
        "start_mode": START_MODE,
        "created_at": now_str(),
    })


def has_local_mp4_outputs(root):
    if not root.exists():
        return False
    return any(path.is_file() for path in root.rglob("*.mp4"))


def remove_dir_if_empty(path):
    if not path.exists() or not path.is_dir():
        return
    try:
        next(path.iterdir())
        return
    except StopIteration:
        path.rmdir()


def show_process_running(show_name):
    escaped_show = re.escape(show_name)
    patterns = [
        f"transmission-cli.*{escaped_show}",
        f"ffmpeg.*{escaped_show}",
        f"media-downloader.py {escaped_show}",
    ]
    for pattern in patterns:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != str(os.getpid())]
        if pids:
            return True
    return False


def transmission_daemon_conflicts_with_show():
    result = subprocess.run(["pgrep", "-f", "transmission-daemon"], capture_output=True, text=True)
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not pids:
        return []

    paths_to_check = [
        str(TMP_DIR),
        str(BASE_DIR / SHOW_NAME),
        str(FINAL_DIR.parent),
    ]
    output = subprocess.run(["lsof", "-Fn", "-p", ",".join(pids)], capture_output=True, text=True)
    conflicts = []
    for line in output.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:]
        if any(path == root or path.startswith(root + os.sep) for root in paths_to_check):
            conflicts.append(path)
    return sorted(set(conflicts))


def ensure_transmission_cli_only():
    if START_MODE != "download":
        return
    conflicts = transmission_daemon_conflicts_with_show()
    if conflicts:
        sample = conflicts[0]
        raise RuntimeError(f"检测到系统级 transmission-daemon 正在占用同剧名目录，请先停止或移除该 torrent: {sample}")


def kill_show_processes(show_name):
    for pattern in [
        f"media-downloader.py.*{re.escape(show_name)}",
        f"transmission-cli.*{re.escape(show_name)}",
    ]:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        pids = [l.strip() for l in result.stdout.splitlines() if l.strip() and l.strip() != str(os.getpid())]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
    deadline = time.time() + 15
    while time.time() < deadline:
        if not show_process_running(show_name):
            return True
        time.sleep(1)
    for pattern in [
        f"media-downloader.py.*{re.escape(show_name)}",
        f"transmission-cli.*{re.escape(show_name)}",
    ]:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        pids = [l.strip() for l in result.stdout.splitlines() if l.strip() and l.strip() != str(os.getpid())]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    time.sleep(1)
    return not show_process_running(show_name)


def resolve_adopt_source(show_name, source_arg):
    if source_arg:
        p = Path(source_arg)
        if p.is_dir():
            return p
        if p.is_file() and is_video(p):
            return p.parent
        raise RuntimeError(f"adopt 来源目录不存在或不含视频: {source_arg}")
    candidates = []
    for base in [Path.home() / "Downloads", BASE_DIR]:
        for d in sorted(base.glob(f"*{show_name}*")):
            if d.is_dir() and any(is_video(f) for f in d.rglob("*") if f.is_file()):
                candidates.append(d)
    if not candidates:
        raise RuntimeError(f"未找到 {show_name} 的手动下载目录，请指定来源路径")
    return candidates[0]


def adopt_phase(state):
    state["phase"] = "adopting"
    set_current_activity(state, "stopping_existing", SHOW_NAME)
    update_status(state)

    if show_process_running(SHOW_NAME):
        log(f"停止现有下载任务: {SHOW_NAME}")
        if not kill_show_processes(SHOW_NAME):
            raise RuntimeError(f"无法停止现有任务: {SHOW_NAME}")
        log("现有任务已停止")
    else:
        log("没有运行中的任务需要停止")

    source_dir = resolve_adopt_source(SHOW_NAME, ADOPT_SOURCE)
    log(f"adopt 来源: {source_dir}")

    for item in list(TMP_DIR.rglob("*")) if TMP_DIR.exists() else []:
        if item.is_file() and item.name.endswith(".part"):
            log(f"清理残留 .part: {item.name}")
            item.unlink(missing_ok=True)

    source_videos = iter_video_files(source_dir)
    if not source_videos:
        raise RuntimeError(f"来源目录无有效视频: {source_dir}")

    moved = 0
    for src in source_videos:
        if src.stat().st_size < MIN_SOURCE_VIDEO_BYTES:
            log(f"跳过过小文件: {src.name}")
            continue
        dst = TMP_DIR / src.name
        if dst.exists():
            if same_size(src, dst):
                log(f"目标已存在相同文件，跳过: {src.name}")
                continue
            dst.unlink(missing_ok=True)
        try:
            os.rename(str(src), str(dst))
            log(f"移动: {src.name} → tmp_{SHOW_NAME}/")
            moved += 1
        except OSError:
            shutil.copy2(str(src), str(dst))
            src.unlink(missing_ok=True)
            log(f"复制(跨卷): {src.name} → tmp_{SHOW_NAME}/")
            moved += 1

    if moved == 0:
        raise RuntimeError("没有新文件需要 adopt")

    log(f"adopt 完成，导入 {moved} 个视频文件，开始后续处理")
    state["adopt_source"] = str(source_dir)
    state["download_complete"] = True
    set_current_activity(state, "adopted", SHOW_NAME)
    update_status(state)


def cleanup_stale_task_paths(download_dir, final_dir, show_state_dir):
    if download_dir.exists():
        shutil.rmtree(download_dir)
    if show_state_dir.exists():
        shutil.rmtree(show_state_dir)
    remove_dir_if_empty(final_dir)
    remove_dir_if_empty(final_dir.parent)


def launch_resumed_download(show_name, magnet):
    log_file = Path(f"/tmp/media-downloader-{show_name}.log")
    env = {
        **os.environ,
        "MEDIA_DOWNLOADER_SKIP_PREFLIGHT": "1",
        "LC_ALL": "en_US.UTF-8",
        "LANG": "en_US.UTF-8",
    }
    with open(os.devnull, "rb") as devnull, open(log_file, "ab", buffering=0) as log_handle:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), show_name, magnet],
            stdin=devnull,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(SKILL_DIR),
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    return proc.pid


def handle_stale_downloads_before_start():
    if START_MODE != "download" or os.environ.get("MEDIA_DOWNLOADER_SKIP_PREFLIGHT") == "1":
        return

    now_ts = time.time()

    def apply_preflight(all_state):
        changed = False
        for stale_show, stale_state in list(all_state.items()):
            if stale_show == SHOW_NAME or not isinstance(stale_state, dict):
                continue
            if stale_state.get("start_mode") != "download":
                continue
            if stale_state.get("phase") == "done":
                continue

            started_at = parse_status_timestamp(stale_state.get("start")) or parse_status_timestamp(stale_state.get("updated_at"))
            if not started_at or now_ts - started_at < STALE_TASK_HOURS * 3600:
                continue
            if show_process_running(stale_show):
                continue

            download_dir = Path(stale_state.get("download_dir") or (BASE_DIR / f"tmp_{stale_show}"))
            final_dir = Path(stale_state.get("final_dir") or (BASE_DIR / stale_show / SEASON_NAME))
            show_state_dir = Path(RUNTIME_STATE_ROOT / stale_show)
            has_payload = has_download_payload(download_dir)
            has_partial = any_partial_files(download_dir)
            has_output = has_local_mp4_outputs(final_dir)

            if not has_payload and not has_partial and not has_output:
                cleanup_stale_task_paths(download_dir, final_dir, show_state_dir)
                all_state.pop(stale_show, None)
                changed = True
                log(f"已清理超过 {STALE_TASK_HOURS} 小时且无下载内容的任务: {stale_show}")
                continue

            if stale_state.get("phase") not in STALE_RESUME_PHASES:
                log(f"跳过自动恢复，当前阶段不属于下载中断态: {stale_show} phase={stale_state.get('phase')}")
                continue
            if not has_payload and not has_partial:
                log(f"跳过自动恢复，下载目录无可恢复内容: {stale_show}")
                continue

            meta = read_download_meta(download_dir)
            magnet = meta.get("magnet") if isinstance(meta, dict) else ""
            if not magnet:
                log(f"跳过自动恢复，缺少下载元数据: {stale_show}")
                continue

            stale_state["phase"] = "starting"
            stale_state["stop_requested"] = False
            stale_state["current_operation"] = "queued_resume"
            stale_state["current_file"] = stale_show
            stale_state["updated_at"] = now_str()
            stale_state["last_error"] = ""
            stale_state["counts"] = stale_state.get("counts") if isinstance(stale_state.get("counts"), dict) else {
                "source_videos": 0,
                "partial_videos": 0,
                "ignored_small": 0,
                "stable_ready": 0,
                "pending_transcodes": 0,
                "local_mp4": 0,
                "nas_mp4": 0,
                "retry_waiting": 0,
                "failed": 0,
            }
            launch_resumed_download(stale_show, magnet)
            changed = True
            log(f"已重启超过 {STALE_TASK_HOURS} 小时未完成的任务: {stale_show}")

        return changed

    mutate_status_map(apply_preflight)


def update_status(state):
    state["updated_at"] = now_str()
    with status_file_lock():
        all_state = load_status_map_unlocked()
        all_state[SHOW_NAME] = state
        atomic_write_json(STATUS_FILE, all_state)


def clear_completed_status_entry(state):
    with status_file_lock():
        all_state = load_status_map_unlocked()
        current = all_state.get(SHOW_NAME)
        if current is state or current:
            all_state.pop(SHOW_NAME, None)
            atomic_write_json(STATUS_FILE, all_state)


def build_state():
    existing = load_status_map().get(SHOW_NAME, {})
    files = existing.get("files") if isinstance(existing.get("files"), dict) else {}
    existing_strategy = existing.get("recovery_strategy")
    recovery_strategy = existing_strategy if existing_strategy in {"resume", "overwrite"} else "resume"
    plex_refresh = existing.get("plex_refresh") if isinstance(existing.get("plex_refresh"), dict) else {"status": "pending", "mode": "", "detail": ""}
    return {
        "show": SHOW_NAME,
        "start_mode": existing.get("start_mode", START_MODE),
        "phase": existing.get("phase", "starting"),
        "start": existing.get("start", now_str()),
        "download_dir": str(TMP_DIR),
        "final_dir": str(FINAL_DIR),
        "nas_dir": str(NAS_SHOW_DIR),
        "download_complete": existing.get("download_complete", False),
        "stop_requested": False,
        "current_operation": existing.get("current_operation", "starting"),
        "current_file": existing.get("current_file", ""),
        "counts": existing.get("counts", {
            "source_videos": 0,
            "partial_videos": 0,
            "ignored_small": 0,
            "stable_ready": 0,
            "pending_transcodes": 0,
            "local_mp4": 0,
            "nas_mp4": 0,
            "retry_waiting": 0,
            "failed": 0,
        }),
        "files": files,
        "last_error": existing.get("last_error", ""),
        "recovery_strategy": recovery_strategy,
        "active_profile": ACTIVE_PROFILE_NAME or "default",
        "media_type": MEDIA_TYPE,
        "plex_refresh": {
            "status": plex_refresh.get("status", "pending"),
            "mode": plex_refresh.get("mode", ""),
            "detail": plex_refresh.get("detail", ""),
        },
    }


def read_json_file(path):
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_skill_config():
    data = read_json_file(SKILL_CONFIG_FILE)
    return data if isinstance(data, dict) else {}


def first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def nested_dict_get(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def coerce_path(value, fallback):
    candidate = value or fallback
    return Path(candidate).expanduser()


def coerce_int(value, fallback, minimum=0):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return fallback
    return parsed if parsed >= minimum else fallback


def coerce_bool(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def parse_show_name_and_year(name):
    match = re.search(r"^(?P<title>.+?)\s*\((?P<year>19\d{2}|20\d{2})\)\s*$", name.strip())
    if not match:
        return name.strip(), None
    return match.group("title").strip(), int(match.group("year"))


def normalize_title(text):
    return re.sub(r"[\s\-_:：·•'\"“”‘’()\[\]（）,.!?！？]", "", (text or "")).casefold()


def score_tvmaze_show(show, query_title):
    names = [show.get("name", "")]
    for key in ["language", "type"]:
        _ = show.get(key)
    externals = show.get("externals") if isinstance(show, dict) else None
    if isinstance(show.get("webChannel"), dict):
        pass
    normalized_query = normalize_title(query_title)
    score = 0
    for name in names:
        normalized_name = normalize_title(name)
        if normalized_name == normalized_query:
            score = max(score, 100)
        elif normalized_query and normalized_query in normalized_name:
            score = max(score, 70)
    summary = normalize_title(show.get("summary", ""))
    if normalized_query and normalized_query in summary:
        score = max(score, 60)
    premiered = str(show.get("premiered") or "")
    if premiered[:4].isdigit():
        score += 5
    if show.get("image"):
        score += 5
    return score


def select_tvmaze_show(payload, query_title):
    candidates = []
    for item in payload if isinstance(payload, list) else []:
        show = item.get("show") if isinstance(item, dict) else None
        if not isinstance(show, dict):
            continue
        score = score_tvmaze_show(show, query_title)
        if score > 0:
            candidates.append((score, show))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1] if candidates else None


def fetch_tvmaze_year(show_name):
    title, _ = parse_show_name_and_year(show_name)
    if not title:
        return None
    try:
        url = "https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(title)
        request = urllib.request.Request(url, headers=TVMAZE_IMAGE_HEADERS)
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    show = select_tvmaze_show(payload, title)
    if not show:
        return None
    premiered = str(show.get("premiered") or "")
    year_text = premiered[:4]
    return int(year_text) if year_text.isdigit() else None


def fetch_tvmaze_show(show_name):
    title, _ = parse_show_name_and_year(show_name)
    if not title:
        return None
    try:
        url = "https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(title)
        request = urllib.request.Request(url, headers=TVMAZE_IMAGE_HEADERS)
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    return select_tvmaze_show(payload, title)


def infer_show_year(show_name):
    title, inline_year = parse_show_name_and_year(show_name)
    if inline_year:
        return inline_year

    if NAS_ROOT.exists():
        for path in sorted(NAS_ROOT.iterdir()):
            if not path.is_dir():
                continue
            existing_title, existing_year = parse_show_name_and_year(path.name)
            if normalize_title(existing_title) == normalize_title(title) and existing_year:
                return existing_year

    return fetch_tvmaze_year(title)


def canonical_show_name(show_name):
    title, inline_year = parse_show_name_and_year(show_name)
    year = inline_year or infer_show_year(show_name)
    return f"{title} ({year})" if year else title


def plex_episode_filename(show_name, season, episode, suffix):
    return f"{show_name} - S{season:02d}E{episode:02d}{suffix.lower()}"


def sanitize_xml_text(value):
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_episode_token(name):
    match = re.search(r"[Ss](?P<season>\d{1,2})[ ._-]?[Ee](?P<episode>\d{1,3})", name)
    if not match:
        return None, None
    return int(match.group("season")), int(match.group("episode"))


def canonical_episode_stem(show_name, season, episode):
    return f"{show_name} - S{season:02d}E{episode:02d}"


def update_nas_paths(state):
    state["final_dir"] = str(FINAL_DIR)
    state["nas_dir"] = str(NAS_SHOW_DIR)
    for entry in state["files"].values():
        output_path = Path(entry.get("output_path", ""))
        if output_path.name:
            entry["output_path"] = str(FINAL_DIR / output_path.name)
            entry["nas_path"] = str(nas_path_for(FINAL_DIR / output_path.name))


def prepare_show_metadata(state):
    global CANONICAL_SHOW_NAME, NAS_SHOW_DIR, FINAL_DIR
    CANONICAL_SHOW_NAME = SHOW_NAME
    if MEDIA_TYPE == "movie":
        FINAL_DIR = BASE_DIR / CANONICAL_SHOW_NAME
        NAS_SHOW_DIR = NAS_ROOT / CANONICAL_SHOW_NAME
        log_label = "电影目录名"
    else:
        FINAL_DIR = BASE_DIR / CANONICAL_SHOW_NAME / SEASON_NAME
        NAS_SHOW_DIR = NAS_ROOT / CANONICAL_SHOW_NAME / SEASON_NAME
        log_label = "剧集目录名"
    update_nas_paths(state)
    if CANONICAL_SHOW_NAME != SHOW_NAME:
        log(f"已解析{log_label}: {SHOW_NAME} → {CANONICAL_SHOW_NAME}")
    else:
        log(f"使用{log_label}: {CANONICAL_SHOW_NAME}")


def find_existing_local_show_dir(show_name):
    title, expected_year = parse_show_name_and_year(show_name)
    normalized_title = normalize_title(title)
    candidates = []

    if not BASE_DIR.exists():
        return None

    for path in sorted(BASE_DIR.iterdir()):
        if not path.is_dir() or path.name.startswith(("tmp_", ".")):
            continue
        season_dir = path / SEASON_NAME
        if not season_dir.is_dir():
            continue
        existing_title, existing_year = parse_show_name_and_year(path.name)
        if normalize_title(existing_title) != normalized_title:
            continue
        if expected_year and existing_year and existing_year != expected_year:
            continue
        media_count = len(list(season_dir.glob("*.mp4")))
        score = media_count * 1000
        if path.name == CANONICAL_SHOW_NAME:
            score += 100
        if path.name == SHOW_NAME:
            score += 50
        if existing_year == expected_year and expected_year:
            score += 20
        if existing_year is not None:
            score += 10
        candidates.append((score, path))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].name))
    return candidates[0][1]


def adopt_existing_local_show_dir():
    target_show_dir = FINAL_DIR.parent
    if FINAL_DIR.exists() and any(FINAL_DIR.glob("*.mp4")):
        return FINAL_DIR

    existing_show_dir = find_existing_local_show_dir(CANONICAL_SHOW_NAME) or find_existing_local_show_dir(SHOW_NAME)
    if not existing_show_dir:
        return FINAL_DIR

    existing_season_dir = existing_show_dir / SEASON_NAME
    if existing_season_dir == FINAL_DIR:
        return FINAL_DIR

    if not target_show_dir.exists():
        target_show_dir.parent.mkdir(parents=True, exist_ok=True)
        existing_show_dir.rename(target_show_dir)
        log(f"已迁移本地剧集目录: {existing_show_dir.name} → {target_show_dir.name}")
        return FINAL_DIR

    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in sorted(existing_season_dir.glob("*")):
        target_path = FINAL_DIR / path.name
        if target_path.exists():
            continue
        path.rename(target_path)
        moved += 1

    for name in ["tvshow.nfo", "poster.jpg", "fanart.jpg"]:
        source_path = existing_show_dir / name
        target_path = target_show_dir / name
        if source_path.exists() and not target_path.exists():
            source_path.rename(target_path)

    try:
        existing_season_dir.rmdir()
    except OSError:
        pass
    try:
        existing_show_dir.rmdir()
    except OSError:
        pass

    log(f"已合并本地剧集目录到规范路径: {target_show_dir.name}，迁移 {moved} 个文件")
    return FINAL_DIR


def normalize_episode_filenames(season_dir, show_name):
    renamed = 0
    for path in sorted(season_dir.glob("*")):
        if not path.is_file():
            continue
        season, episode = extract_episode_token(path.name)
        if season is None or episode is None:
            continue
        suffix = "".join(path.suffixes) if path.suffixes else path.suffix
        new_stem = canonical_episode_stem(show_name, season, episode)
        new_name = f"{new_stem}{suffix.lower()}"
        new_path = path.with_name(new_name)
        if new_path == path or new_path.exists():
            continue
        path.rename(new_path)
        renamed += 1
    return renamed


def write_tvshow_nfo(show_dir, show_name):
    title, year = parse_show_name_and_year(show_name)
    nfo_path = show_dir / "tvshow.nfo"
    premiered = f"{year}-01-01" if year else ""
    xml = "\n".join([
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<tvshow>",
        f"  <title>{sanitize_xml_text(title)}</title>",
        f"  <originaltitle>{sanitize_xml_text(title)}</originaltitle>",
        f"  <sorttitle>{sanitize_xml_text(title)}</sorttitle>",
        f"  <year>{year or ''}</year>",
        f"  <premiered>{premiered}</premiered>",
        "</tvshow>",
        "",
    ])
    nfo_path.write_text(xml, encoding="utf-8")


def write_movie_nfo(show_dir, show_name):
    title, year = parse_show_name_and_year(show_name)
    nfo_path = show_dir / "movie.nfo"
    premiered = f"{year}-01-01" if year else ""
    xml = "\n".join([
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<movie>",
        f"  <title>{sanitize_xml_text(title)}</title>",
        f"  <originaltitle>{sanitize_xml_text(title)}</originaltitle>",
        f"  <sorttitle>{sanitize_xml_text(title)}</sorttitle>",
        f"  <year>{year or ''}</year>",
        f"  <premiered>{premiered}</premiered>",
        "</movie>",
        "",
    ])
    nfo_path.write_text(xml, encoding="utf-8")


def download_file(url, target_path):
    request = urllib.request.Request(url, headers={"User-Agent": "media-downloader/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    if not data:
        raise RuntimeError(f"empty download: {url}")
    tmp_path = target_path.with_suffix(target_path.suffix + ".partial")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "wb") as fh:
        fh.write(data)
    os.replace(tmp_path, target_path)


def ensure_show_artwork(show_dir, show_name):
    show = fetch_tvmaze_show(show_name)
    if not show:
        log(f"未找到可用海报源: {show_name}")
        return
    images = show.get("image") if isinstance(show, dict) else None
    if not isinstance(images, dict):
        log(f"剧集未提供海报信息: {show_name}")
        return

    poster_url = images.get("original") or images.get("medium")
    fanart_url = None
    externals = show.get("externals") if isinstance(show, dict) else None
    if isinstance(externals, dict) and externals.get("thetvdb"):
        fanart_url = None

    if poster_url and not (show_dir / "poster.jpg").exists():
        try:
            download_file(poster_url, show_dir / "poster.jpg")
            log(f"已下载海报: {show_dir / 'poster.jpg'}")
        except Exception as exc:
            log(f"下载海报失败: {exc}")

    if fanart_url and not (show_dir / "fanart.jpg").exists():
        try:
            download_file(fanart_url, show_dir / "fanart.jpg")
            log(f"已下载背景图: {show_dir / 'fanart.jpg'}")
        except Exception as exc:
            log(f"下载背景图失败: {exc}")


def organize_local_show(state):
    show_dir = FINAL_DIR.parent
    show_dir.mkdir(parents=True, exist_ok=True)
    if MEDIA_TYPE == "movie":
        write_movie_nfo(show_dir, CANONICAL_SHOW_NAME)
        ensure_show_artwork(show_dir, CANONICAL_SHOW_NAME)
        log(f"已整理本地电影目录: {CANONICAL_SHOW_NAME}，写入 movie.nfo")
    else:
        renamed = normalize_episode_filenames(FINAL_DIR, CANONICAL_SHOW_NAME)
        write_tvshow_nfo(show_dir, CANONICAL_SHOW_NAME)
        ensure_show_artwork(show_dir, CANONICAL_SHOW_NAME)
        log(f"已整理本地剧集目录: {CANONICAL_SHOW_NAME}，重命名 {renamed} 个文件，写入 tvshow.nfo")


def configure_runtime_paths():
    global BASE_DIR, SEASON_NAME, TMP_DIR, FINAL_DIR, NAS_ROOT, CANONICAL_SHOW_NAME, NAS_SHOW_DIR, RUNTIME_STATE_ROOT, SHOW_STATE_DIR, TRANSMISSION_CONFIG_DIR, TRANSMISSION_UPLOAD_LIMIT_KBPS, SEED_IDLE_SECONDS, STABLE_CHECK_SECONDS, STABLE_POLLS, DOWNLOAD_SETTLE_POLLS, TIMEOUT_HOURS, MIN_SOURCE_VIDEO_BYTES, SKILL_CONFIG, NAS_AUTO_MOUNT, NAS_MOUNT_URL
    SKILL_CONFIG = load_skill_config()
    config = read_json_file(OPENCLAW_CONFIG)
    skill_config = nested_dict_get(config, "skills", "media-downloader")
    media_config = nested_dict_get(config, "mediaDownloader")
    skill_config = skill_config if isinstance(skill_config, dict) else {}
    media_config = media_config if isinstance(media_config, dict) else {}
    skill_local = SKILL_CONFIG if isinstance(SKILL_CONFIG, dict) else {}
    nas_config = skill_local.get("nas") if isinstance(skill_local.get("nas"), dict) else {}

    BASE_DIR = coerce_path(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_BASE_DIR"),
            skill_local.get("baseDir"),
            skill_config.get("baseDir"),
            media_config.get("baseDir"),
            str(DEFAULT_BASE_DIR),
        ),
        DEFAULT_BASE_DIR,
    )
    NAS_ROOT = coerce_path(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_TARGET_DIR"),
            os.environ.get("MEDIA_DOWNLOADER_NAS_ROOT"),
            skill_local.get("targetDir"),
            skill_local.get("nasRoot"),
            nas_config.get("root"),
            skill_config.get("nasRoot"),
            media_config.get("nasRoot"),
            str(DEFAULT_NAS_ROOT),
        ),
        DEFAULT_NAS_ROOT,
    )
    SEASON_NAME = first_value(
        os.environ.get("MEDIA_DOWNLOADER_SEASON_NAME"),
        skill_local.get("seasonName"),
        skill_config.get("seasonName"),
        media_config.get("seasonName"),
        DEFAULT_SEASON_NAME,
    )
    runtime_state_root_value = first_value(
        os.environ.get("MEDIA_DOWNLOADER_STATE_DIR"),
        skill_local.get("stateDir"),
        skill_config.get("stateDir"),
        media_config.get("stateDir"),
        str(DEFAULT_RUNTIME_STATE_ROOT),
    )
    TRANSMISSION_UPLOAD_LIMIT_KBPS = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_UPLOAD_LIMIT_KBPS"),
            skill_local.get("uploadLimitKbps"),
            skill_config.get("uploadLimitKbps"),
            media_config.get("uploadLimitKbps"),
            DEFAULT_TRANSMISSION_UPLOAD_LIMIT_KBPS,
        ),
        DEFAULT_TRANSMISSION_UPLOAD_LIMIT_KBPS,
        minimum=1,
    )
    SEED_IDLE_SECONDS = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_SEED_IDLE_SECONDS"),
            skill_local.get("seedIdleSeconds"),
            skill_config.get("seedIdleSeconds"),
            media_config.get("seedIdleSeconds"),
            DEFAULT_SEED_IDLE_SECONDS,
        ),
        DEFAULT_SEED_IDLE_SECONDS,
        minimum=0,
    )
    STABLE_CHECK_SECONDS = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_STABLE_CHECK_SECONDS"),
            skill_local.get("stableCheckSeconds"),
            DEFAULT_STABLE_CHECK_SECONDS,
        ),
        DEFAULT_STABLE_CHECK_SECONDS,
        minimum=1,
    )
    STABLE_POLLS = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_STABLE_POLLS"),
            skill_local.get("stablePolls"),
            DEFAULT_STABLE_POLLS,
        ),
        DEFAULT_STABLE_POLLS,
        minimum=1,
    )
    DOWNLOAD_SETTLE_POLLS = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_DOWNLOAD_SETTLE_POLLS"),
            skill_local.get("downloadSettlePolls"),
            DEFAULT_DOWNLOAD_SETTLE_POLLS,
        ),
        DEFAULT_DOWNLOAD_SETTLE_POLLS,
        minimum=1,
    )
    TIMEOUT_HOURS = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_TIMEOUT_HOURS"),
            skill_local.get("timeoutHours"),
            DEFAULT_TIMEOUT_HOURS,
        ),
        DEFAULT_TIMEOUT_HOURS,
        minimum=1,
    )
    MIN_SOURCE_VIDEO_BYTES = coerce_int(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_MIN_SOURCE_VIDEO_BYTES"),
            skill_local.get("minSourceVideoBytes"),
            DEFAULT_MIN_SOURCE_VIDEO_BYTES,
        ),
        DEFAULT_MIN_SOURCE_VIDEO_BYTES,
        minimum=1,
    )
    NAS_AUTO_MOUNT = coerce_bool(
        first_value(
            os.environ.get("MEDIA_DOWNLOADER_NAS_AUTO_MOUNT"),
            skill_local.get("nasAutoMount"),
            nas_config.get("autoMount"),
            DEFAULT_NAS_AUTO_MOUNT,
        ),
        DEFAULT_NAS_AUTO_MOUNT,
    )
    NAS_MOUNT_URL = first_value(
        os.environ.get("MEDIA_DOWNLOADER_NAS_MOUNT_URL"),
        skill_local.get("nasMountUrl"),
        nas_config.get("mountUrl"),
        "",
    )
    RUNTIME_STATE_ROOT = coerce_path(runtime_state_root_value, DEFAULT_RUNTIME_STATE_ROOT)
    TMP_DIR = BASE_DIR / f"tmp_{SHOW_NAME}"
    CANONICAL_SHOW_NAME = SHOW_NAME

    load_profile()
    profile_nas_root = ACTIVE_PROFILE.get("targetDir") or ACTIVE_PROFILE.get("nasRoot")
    if profile_nas_root and not first_value(os.environ.get("MEDIA_DOWNLOADER_TARGET_DIR"), os.environ.get("MEDIA_DOWNLOADER_NAS_ROOT")):
        NAS_ROOT = Path(profile_nas_root)

    if MEDIA_TYPE == "movie":
        FINAL_DIR = BASE_DIR / SHOW_NAME / SHOW_NAME
        NAS_SHOW_DIR = NAS_ROOT / CANONICAL_SHOW_NAME
    else:
        FINAL_DIR = BASE_DIR / SHOW_NAME / SEASON_NAME
        NAS_SHOW_DIR = NAS_ROOT / CANONICAL_SHOW_NAME / SEASON_NAME
    SHOW_STATE_DIR = RUNTIME_STATE_ROOT / SHOW_NAME
    TRANSMISSION_CONFIG_DIR = SHOW_STATE_DIR / "transmission"


def load_plex_config():
    config = read_json_file(OPENCLAW_CONFIG)
    skill_config = nested_dict_get(config, "skills", "media-downloader")
    media_config = nested_dict_get(config, "mediaDownloader")
    plex_config = nested_dict_get(config, "plex")
    env_config = nested_dict_get(config, "env")
    skill_local = SKILL_CONFIG if isinstance(SKILL_CONFIG, dict) else {}

    skill_config = skill_config if isinstance(skill_config, dict) else {}
    media_config = media_config if isinstance(media_config, dict) else {}
    plex_config = plex_config if isinstance(plex_config, dict) else {}
    env_config = env_config if isinstance(env_config, dict) else {}

    server = first_value(
        os.environ.get("PLEX_SERVER"),
        skill_local.get("plexServer"),
        skill_config.get("plexServer"),
        media_config.get("plexServer"),
        plex_config.get("server"),
        env_config.get("PLEX_SERVER"),
    )
    token = first_value(
        os.environ.get("PLEX_TOKEN"),
        skill_local.get("plexToken"),
        skill_config.get("plexToken"),
        media_config.get("plexToken"),
        plex_config.get("token"),
        env_config.get("PLEX_TOKEN"),
    )
    return server, token


def resolve_recovery_strategy():
    config = read_json_file(OPENCLAW_CONFIG)
    skill_config = nested_dict_get(config, "skills", "media-downloader")
    media_config = nested_dict_get(config, "mediaDownloader")
    skill_local = SKILL_CONFIG if isinstance(SKILL_CONFIG, dict) else {}
    skill_config = skill_config if isinstance(skill_config, dict) else {}
    media_config = media_config if isinstance(media_config, dict) else {}

    strategy = first_value(
        os.environ.get("MEDIA_DOWNLOADER_RECOVERY"),
        skill_local.get("recoveryStrategy"),
        skill_config.get("recoveryStrategy"),
        media_config.get("recoveryStrategy"),
        "resume",
    )
    return strategy if strategy in {"resume", "overwrite"} else "resume"


def acquire_show_lock(wait_seconds=15):
    global RUN_LOCK_HANDLE
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    deadline = time.time() + wait_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            RUN_LOCK_HANDLE = handle
            return
        except BlockingIOError:
            if time.time() >= deadline:
                handle.close()
                raise RuntimeError(f"任务已在运行中: {SHOW_NAME}")
            time.sleep(1)


def release_show_lock():
    global RUN_LOCK_HANDLE
    if not RUN_LOCK_HANDLE:
        return
    try:
        fcntl.flock(RUN_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        RUN_LOCK_HANDLE.close()
    except Exception:
        pass
    RUN_LOCK_HANDLE = None
    LOCK_FILE.unlink(missing_ok=True)


def reset_existing_outputs(state):
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    if FINAL_DIR.exists():
        shutil.rmtree(FINAL_DIR)
    if SHOW_STATE_DIR.exists():
        shutil.rmtree(SHOW_STATE_DIR)
    state["files"] = {}
    state["counts"] = {
        "source_videos": 0,
        "partial_videos": 0,
        "ignored_small": 0,
        "stable_ready": 0,
        "pending_transcodes": 0,
        "local_mp4": 0,
        "nas_mp4": 0,
        "retry_waiting": 0,
        "failed": 0,
    }
    state["download_complete"] = False
    state["last_error"] = ""
    state["plex_refresh"] = {"status": "pending", "mode": "", "detail": ""}


def prepare_run_environment(state):
    strategy = resolve_recovery_strategy()
    state["recovery_strategy"] = strategy
    if strategy == "overwrite":
        log(f"恢复策略: overwrite，清理已有输出: {SHOW_NAME}")
        reset_existing_outputs(state)


def set_current_activity(state, operation, target=""):
    state["current_operation"] = operation
    state["current_file"] = target


def clear_current_activity(state, operation="idle"):
    set_current_activity(state, operation, "")


def extract_episode_info(source_path):
    season = None
    episode = None
    search_texts = [source_path.stem, source_path.name, str(source_path.parent.name)]

    for text in search_texts:
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            if season is None and match.groupdict().get("season"):
                season = int(match.group("season"))
            if episode is None and match.groupdict().get("episode"):
                episode = int(match.group("episode"))
            if episode is not None:
                break
        if episode is not None:
            break

    if episode is not None and season is None:
        season = 1

    episode_label = f"S{season:02d}E{episode:02d}" if episode is not None else ""
    display_name = f"{episode_label} {source_path.name}".strip() if episode_label else source_path.name
    sort_key = [season if season is not None else 999, episode if episode is not None else 9999, source_path.name.lower()]
    return {
        "season": season,
        "episode": episode,
        "episode_label": episode_label,
        "display_name": display_name,
        "sort_key": sort_key,
    }


def retry_delay_for_attempt(attempt_number):
    index = min(max(attempt_number - 1, 0), len(RETRY_DELAYS) - 1)
    return RETRY_DELAYS[index]


def retry_ready(entry):
    next_retry_at = entry.get("next_retry_at")
    return not next_retry_at or time.time() >= float(next_retry_at)


def schedule_retry(entry, reason):
    attempts = int(entry.get("attempts", 0))
    entry["last_error"] = reason
    if attempts >= MAX_TRANSCODE_ATTEMPTS:
        entry["state"] = "failed"
        entry["next_retry_at"] = None
        entry["retry_delay"] = 0
        return False

    delay = retry_delay_for_attempt(attempts)
    entry["state"] = "retry_wait"
    entry["next_retry_at"] = time.time() + delay
    entry["retry_delay"] = delay
    return True


def entry_has_local_output(entry):
    output_path = Path(entry.get("output_path", ""))
    return output_path.exists() and output_path.stat().st_size > 0


def pending_transcode_entries(state):
    pending = []
    for entry in state["files"].values():
        source_path = Path(entry.get("source_path", ""))
        if entry.get("state") in {"copied", "ignored_small"}:
            continue
        if entry_has_local_output(entry):
            continue
        if entry.get("state") == "failed":
            continue
        if source_path.exists() or entry.get("state") in {"retry_wait", "transcoding", "seen", "missing_source", "partial_only"}:
            pending.append(entry)
    return pending


def next_retry_deadline(state):
    retry_times = []
    for entry in pending_transcode_entries(state):
        if entry.get("state") == "retry_wait" and entry.get("next_retry_at"):
            retry_times.append(float(entry["next_retry_at"]))
    return min(retry_times) if retry_times else None


def ensure_transcodes_complete(state):
    scan_downloads(state)
    failed = []
    pending = []
    invalid_outputs = []
    local_outputs = sorted(FINAL_DIR.glob("*.mp4")) if FINAL_DIR.exists() else []

    for entry in state["files"].values():
        name = entry.get("display_name") or Path(entry.get("source_path", "unknown")).name
        if entry.get("state") == "failed":
            failed.append(name)
        elif entry.get("state") not in {"partial_only", "ignored_small"} and not entry_has_local_output(entry):
            pending.append(name)

    for output_path in local_outputs:
        valid, reason = validate_video_file(output_path, check_stable=True)
        if not valid:
            invalid_outputs.append(f"{output_path.name}({reason})")

    if not local_outputs and not pending and not failed:
        raise RuntimeError("未发现可用视频输出")
    if pending:
        raise RuntimeError(f"仍有未完成转码任务: {', '.join(sorted(pending)[:5])}")
    if failed:
        raise RuntimeError(f"存在转码失败文件: {', '.join(sorted(failed)[:5])}")
    if invalid_outputs:
        raise RuntimeError(f"存在无效 mp4 输出: {', '.join(invalid_outputs[:5])}")


def ensure_nas_sync_complete():
    if not NAS_ROOT.exists():
        raise RuntimeError(f"目标磁盘不可用: {NAS_ROOT}")
    local_outputs = sorted(FINAL_DIR.glob("*.mp4")) if FINAL_DIR.exists() else []
    if not local_outputs:
        raise RuntimeError("本地无可同步 mp4")

    missing = []
    invalid = []
    for output_path in local_outputs:
        # Skip files that are invalid locally (shouldn't happen after ensure_transcodes_complete,
        # but defensive check)
        valid_local, _ = validate_video_file(output_path)
        if not valid_local:
            continue

        target_path = nas_path_for(output_path)
        if not same_size(output_path, target_path):
            missing.append(output_path.name)
            continue
        valid, reason = validate_video_file(target_path)
        if not valid:
            invalid.append(f"{target_path.name}({reason})")
    if missing:
        raise RuntimeError(f"目标磁盘转移未完成: {', '.join(missing[:5])}")
    if invalid:
        raise RuntimeError(f"目标磁盘存在无效 mp4 输出: {', '.join(invalid[:5])}")


def plex_request(server, token, path, params=None):
    params = dict(params or {})
    params["X-Plex-Token"] = token
    url = f"{server}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def common_suffix_score(left_parts, right_parts):
    score = 0
    for left, right in zip(reversed(left_parts), reversed(right_parts)):
        if left != right:
            break
        score += 1
    return score


def find_best_plex_section(root):
    server, token = load_plex_config()
    if not server or not token:
        return None

    try:
        xml_bytes = plex_request(server, token, "/library/sections")
        tree = ET.fromstring(xml_bytes)
    except Exception as exc:
        log(f"Plex section 查询失败: {exc}")
        return None

    root_parts = root.parts
    best = None
    best_score = 0

    for section in tree.findall("Directory"):
        key = section.attrib.get("key")
        if not key:
            continue
        for location in section.findall("Location"):
            remote_path = location.attrib.get("path", "")
            if not remote_path:
                continue
            remote_parts = Path(remote_path).parts
            score = common_suffix_score(root_parts, remote_parts)
            if score > best_score:
                best_score = score
                best = {
                    "key": key,
                    "remote_root": remote_path,
                    "server": server,
                    "token": token,
                }

    return best if best_score > 0 else None


def refresh_plex(state, show_dir):
    section = find_best_plex_section(NAS_ROOT)
    if not section:
        state["plex_refresh"] = {"status": "skipped", "mode": "none", "detail": "section_not_found"}
        log("未找到可匹配的 Plex section，跳过刷新")
        return False

    try:
        relative_show = show_dir.parent.relative_to(NAS_ROOT)
        remote_show = str(Path(section["remote_root"]) / relative_show)
        plex_request(section["server"], section["token"], f"/library/sections/{section['key']}/refresh", {"path": remote_show})
        state["plex_refresh"] = {"status": "triggered", "mode": "path", "detail": remote_show}
        log(f"Plex 已触发定向刷新: section={section['key']} path={remote_show}")
        return True
    except Exception as exc:
        log(f"Plex 定向刷新失败，尝试整库刷新: {exc}")

    try:
        plex_request(section["server"], section["token"], f"/library/sections/{section['key']}/refresh")
        state["plex_refresh"] = {"status": "triggered", "mode": "section", "detail": str(section["key"])}
        log(f"Plex 已触发整库刷新: section={section['key']}")
        return True
    except Exception as exc:
        state["plex_refresh"] = {"status": "failed", "mode": "section", "detail": str(exc)}
        raise RuntimeError(f"Plex 刷新失败: {exc}")


def is_video(path):
    return path.is_file() and path.suffix.lstrip(".").lower() in VIDEO_EXTS


def is_partial_video(path):
    if not path.is_file() or not path.name.endswith(".part"):
        return False
    original_name = path.name[:-5]
    return Path(original_name).suffix.lstrip(".").lower() in VIDEO_EXTS


def iter_partial_video_files(root):
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if is_partial_video(path))


def iter_video_files(root):
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if is_video(path))


def has_partial_sibling(path):
    candidates = [
        path.parent / f"{path.name}.part",
        path.with_suffix(path.suffix + ".part"),
    ]
    return any(candidate.exists() for candidate in candidates)


def any_partial_files(root):
    if not root.exists():
        return False
    return any(path.is_file() and path.name.endswith(".part") for path in root.rglob("*"))


def is_small_source_file(path):
    return path.exists() and path.stat().st_size < MIN_SOURCE_VIDEO_BYTES


def read_recent_log(max_bytes=128 * 1024):
    if not LOG_FILE.exists():
        return ""
    size = LOG_FILE.stat().st_size
    with open(LOG_FILE, "rb") as fh:
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


def transmission_is_seeding():
    text = read_recent_log().lower()
    return ("seeding" in text and "100.0%" in text) or ("uploading to" in text and "100.0%" in text)


def has_download_payload(root):
    if not root.exists():
        return False
    return any(path.is_file() and not path.name.startswith(".") for path in root.rglob("*"))


def probe_video_info(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=COMMAND_ENV,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    format_info = data.get("format") or {}
    try:
        duration = float(format_info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    try:
        size = int(format_info.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size = path.stat().st_size if path.exists() else 0
    return {
        "has_video": any(stream.get("codec_type") == "video" for stream in streams),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "duration": duration,
        "size": size,
    }


def check_moov_atom(path):
    """Check if an mp4 file is structurally readable enough for playback."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration,size",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            env=COMMAND_ENV,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False
        streams = data.get("streams") or []
        format_info = data.get("format") or {}
        if not streams:
            return False
        try:
            duration = float(format_info.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return duration > 0
    except Exception:
        return False


def validate_video_file(path, minimum_bytes=MIN_SOURCE_VIDEO_BYTES, minimum_duration=MIN_EPISODE_DURATION_SECONDS, check_stable=False):
    if not path.exists() or path.stat().st_size < minimum_bytes:
        return False, f"file_too_small:{path.stat().st_size if path.exists() else 0}"
    # Stability check: ensure file is not still being written (size unchanged over STABLE_CHECK_SECONDS)
    if check_stable:
        size1 = path.stat().st_size
        time.sleep(STABLE_CHECK_SECONDS)
        size2 = path.stat().st_size
        if size1 != size2:
            return False, f"file_still_writing:{size1}->{size2}"
    info = probe_video_info(path)
    if not info:
        return False, "ffprobe_failed"
    if not info["has_video"]:
        return False, "no_video_stream"
    if not info["has_audio"]:
        return False, "no_audio_stream"
    if info["duration"] < minimum_duration:
        return False, f"duration_too_short:{int(info['duration'])}"
    # Check moov atom for mp4 files (critical for playability)
    if path.suffix.lower() == ".mp4" and not check_moov_atom(path):
        return False, "moov_atom_missing_or_truncated"
    return True, info


def output_path_for(source_path):
    if MEDIA_TYPE == "movie":
        return FINAL_DIR / f"{CANONICAL_SHOW_NAME}.mp4"
    info = extract_episode_info(source_path)
    if info["season"] is not None and info["episode"] is not None:
        filename = f"{canonical_episode_stem(CANONICAL_SHOW_NAME, info['season'], info['episode'])}.mp4"
    else:
        filename = source_path.with_suffix(".mp4").name
    return FINAL_DIR / filename


def nas_path_for(output_path):
    if MEDIA_TYPE == "movie":
        return NAS_SHOW_DIR / f"{CANONICAL_SHOW_NAME}.mp4"
    season, episode = extract_episode_token(output_path.name)
    if season is not None and episode is not None:
        filename = plex_episode_filename(CANONICAL_SHOW_NAME, season, episode, output_path.suffix)
    else:
        filename = output_path.name
    return NAS_SHOW_DIR / filename


def same_size(left, right):
    return left.exists() and right.exists() and left.stat().st_size == right.stat().st_size


def sidecar_paths_for(video_path):
    return [candidate for candidate in sorted(video_path.parent.glob(f"{video_path.stem}.*")) if candidate.is_file() and candidate != video_path]


def assert_no_nas_overwrite(source_path, target_path):
    if target_path.exists() and not same_size(source_path, target_path):
        raise RuntimeError(f"目标文件已存在且内容不同，禁止自动覆盖: {target_path}")


def copy_file_with_replace(source_path, target_path):
    assert_no_nas_overwrite(source_path, target_path)
    temp_target = target_path.parent / f".{target_path.name}.partial"
    if temp_target.exists():
        temp_target.unlink()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, temp_target)
    os.replace(temp_target, target_path)


def copy_show_sidecars_to_nas():
    show_dir = FINAL_DIR.parent
    target_show_dir = NAS_SHOW_DIR.parent
    for name in ["tvshow.nfo", "poster.jpg", "fanart.jpg"]:
        source_path = show_dir / name
        target_path = target_show_dir / name
        if source_path.exists() and not same_size(source_path, target_path):
            copy_file_with_replace(source_path, target_path)


def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=COMMAND_ENV,
    )
    return result.returncode == 0


def run_logged_process(cmd, operation, target=""):
    global ACTIVE_PROCESS, ACTIVE_OPERATION, ACTIVE_TARGET
    with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as log_handle:
        log_handle.write(f"[{time.strftime('%H:%M:%S')}] CMD[{operation}] {shlex.join(cmd)}\n")
        log_handle.flush()
        ACTIVE_OPERATION = operation
        ACTIVE_TARGET = target
        ACTIVE_PROCESS = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT, env=COMMAND_ENV)
        return_code = ACTIVE_PROCESS.wait()
        ACTIVE_PROCESS = None
        ACTIVE_OPERATION = "idle"
        ACTIVE_TARGET = ""
        return return_code


def terminate_active_process(grace_seconds=15):
    global ACTIVE_PROCESS, ACTIVE_TARGET
    if not ACTIVE_PROCESS or ACTIVE_PROCESS.poll() is not None:
        return
    try:
        ACTIVE_PROCESS.terminate()
        ACTIVE_PROCESS.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        ACTIVE_PROCESS.kill()
    except Exception:
        pass
    finally:
        ACTIVE_TARGET = ""


def cleanup_lingering_processes():
    patterns = [
        f"transmission-cli.*{SHOW_NAME}",
        f"ffmpeg.*{SHOW_NAME}",
        f"media-downloader.py {SHOW_NAME}",
    ]
    for pattern in patterns:
        subprocess.run(["pkill", "-TERM", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def log_lingering_processes():
    checks = {
        "transmission": f"transmission-cli.*{SHOW_NAME}",
        "ffmpeg": f"ffmpeg.*{SHOW_NAME}",
        "python": f"media-downloader.py {SHOW_NAME}",
    }
    remains = []
    for name, pattern in checks.items():
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != str(os.getpid())]
        if pids:
            remains.append(f"{name}={','.join(pids)}")
    if remains:
        log(f"残留进程: {'; '.join(remains)}")
    else:
        log("无残留进程")


def scan_downloads(state):
    files_state = state["files"]
    source_files = iter_video_files(TMP_DIR)
    partial_files = iter_partial_video_files(TMP_DIR)
    current_keys = set()

    for partial_path in partial_files:
        final_name = partial_path.name[:-5]
        final_candidate = partial_path.with_name(final_name)
        key = str(final_candidate.relative_to(TMP_DIR))
        current_keys.add(key)
        episode_info = extract_episode_info(final_candidate)
        entry = files_state.setdefault(key, {
            "source_path": str(final_candidate),
            "output_path": str(output_path_for(final_candidate)),
            "nas_path": str(nas_path_for(output_path_for(final_candidate))),
            "state": "partial_only",
            "attempts": 0,
            "stable_polls": 0,
            "last_error": "",
        })
        entry["source_path"] = str(final_candidate)
        entry["partial_path"] = str(partial_path)
        entry["output_path"] = str(output_path_for(final_candidate))
        entry["nas_path"] = str(nas_path_for(output_path_for(final_candidate)))
        entry["size"] = partial_path.stat().st_size
        entry["mtime"] = int(partial_path.stat().st_mtime)
        entry["source_signature"] = f"partial:{entry['size']}:{entry['mtime']}"
        entry["partial"] = True
        entry["ignored_small"] = entry["size"] < MIN_SOURCE_VIDEO_BYTES
        entry["season"] = episode_info["season"]
        entry["episode"] = episode_info["episode"]
        entry["episode_label"] = episode_info["episode_label"]
        entry["display_name"] = episode_info["display_name"]
        entry["sort_key"] = episode_info["sort_key"]
        if entry.get("state") not in {"transcoded", "copied", "retry_wait", "failed"}:
            entry["state"] = "partial_only"
            entry["stable_polls"] = 0

    for source_path in source_files:
        key = str(source_path.relative_to(TMP_DIR))
        current_keys.add(key)
        episode_info = extract_episode_info(source_path)
        entry = files_state.setdefault(key, {
            "source_path": str(source_path),
            "output_path": str(output_path_for(source_path)),
            "nas_path": str(nas_path_for(output_path_for(source_path))),
            "state": "seen",
            "attempts": 0,
            "stable_polls": 0,
            "last_error": "",
        })

        size = source_path.stat().st_size
        mtime = int(source_path.stat().st_mtime)
        signature = f"{size}:{mtime}"
        previous_signature = entry.get("source_signature")
        partial_exists = has_partial_sibling(source_path)
        ignored_small = size < MIN_SOURCE_VIDEO_BYTES

        entry["source_path"] = str(source_path)
        entry["partial_path"] = ""
        entry["output_path"] = str(output_path_for(source_path))
        entry["nas_path"] = str(nas_path_for(output_path_for(source_path)))
        entry["size"] = size
        entry["mtime"] = mtime
        entry["source_signature"] = signature
        entry["partial"] = partial_exists
        entry["ignored_small"] = ignored_small
        entry["season"] = episode_info["season"]
        entry["episode"] = episode_info["episode"]
        entry["episode_label"] = episode_info["episode_label"]
        entry["display_name"] = episode_info["display_name"]
        entry["sort_key"] = episode_info["sort_key"]

        if ignored_small:
            entry["stable_polls"] = 0
            if entry.get("state") not in {"transcoded", "copied"}:
                entry["state"] = "ignored_small"
            continue

        if previous_signature != signature:
            entry["stable_polls"] = 0
            previous_state = entry.get("state")
            if previous_state in {"failed", "retry_wait", "partial_only", "ignored_small"}:
                entry["state"] = "seen"
                entry["last_error"] = ""
                if previous_state in {"failed", "retry_wait"}:
                    entry["attempts"] = 0
                entry["next_retry_at"] = None
                entry["retry_delay"] = 0
        elif partial_exists:
            entry["stable_polls"] = 0
        elif entry.get("state") not in {"transcoded", "copied"}:
            entry["stable_polls"] = int(entry.get("stable_polls", 0)) + 1

        if entry.get("state") == "retry_wait" and retry_ready(entry):
            entry["state"] = "seen"
            entry["next_retry_at"] = None
            entry["retry_delay"] = 0

        output_path = Path(entry["output_path"])
        nas_path = Path(entry["nas_path"])
        if same_size(output_path, nas_path):
            entry["state"] = "copied"
            entry["next_retry_at"] = None
        elif output_path.exists() and output_path.stat().st_size > 0:
            # Validate that the output file is actually complete, not a leftover partial
            valid_output, output_reason = validate_video_file(output_path)
            if valid_output:
                entry["state"] = "transcoded"
                entry["next_retry_at"] = None
            else:
                # Output file exists but is invalid (truncated/corrupt), reset for re-transcode
                log(f"发现无效输出文件，将重新转码: {output_path.name} ({output_reason})")
                try:
                    output_path.unlink()
                except OSError:
                    pass
                entry["state"] = "seen"
                entry["stable_polls"] = STABLE_POLLS
                entry["attempts"] = 0

    for key, entry in files_state.items():
        if key not in current_keys and not Path(entry.get("source_path", "")).exists() and entry.get("state") not in {"copied", "removed", "ignored_small"}:
            entry["state"] = "missing_source"

    refresh_counts(state)
    return [path for path in source_files if path.stat().st_size >= MIN_SOURCE_VIDEO_BYTES]


def refresh_counts(state):
    source_files = iter_video_files(TMP_DIR)
    source_count = len([path for path in source_files if path.stat().st_size >= MIN_SOURCE_VIDEO_BYTES])
    partial_count = len(iter_partial_video_files(TMP_DIR))
    local_mp4 = len(list(FINAL_DIR.glob("*.mp4"))) if FINAL_DIR.exists() else 0
    nas_mp4 = len(list(NAS_SHOW_DIR.glob("*.mp4"))) if NAS_SHOW_DIR.exists() else 0
    stable_ready = 0
    failed = 0
    retry_waiting = 0
    pending_transcodes = 0
    ignored_small = 0

    for entry in state["files"].values():
        if entry.get("state") == "failed":
            failed += 1
        if entry.get("state") == "retry_wait":
            retry_waiting += 1
        if entry.get("state") == "ignored_small":
            ignored_small += 1
        if entry.get("stable_polls", 0) >= STABLE_POLLS and entry.get("state") not in {"transcoding", "transcoded", "copied", "retry_wait", "ignored_small"}:
            stable_ready += 1
        if entry.get("state") not in {"copied", "failed", "ignored_small"} and not entry_has_local_output(entry):
            pending_transcodes += 1

    state["counts"] = {
        "source_videos": source_count,
        "partial_videos": partial_count,
        "ignored_small": ignored_small,
        "stable_ready": stable_ready,
        "pending_transcodes": pending_transcodes,
        "local_mp4": local_mp4,
        "nas_mp4": nas_mp4,
        "retry_waiting": retry_waiting,
        "failed": failed,
    }


def select_transcode_candidate(state):
    candidates = []
    for key, entry in state["files"].items():
        source_path = Path(entry.get("source_path", ""))
        output_path = Path(entry.get("output_path", ""))
        if not source_path.exists():
            continue
        if output_path.exists() and output_path.stat().st_size > 0:
            continue
        if entry.get("partial"):
            continue
        if entry.get("stable_polls", 0) < STABLE_POLLS:
            continue
        if entry.get("attempts", 0) >= MAX_TRANSCODE_ATTEMPTS:
            continue
        if not retry_ready(entry):
            continue
        if entry.get("size", 0) < MIN_SOURCE_VIDEO_BYTES:
            continue
        if entry.get("state") in {"transcoding", "transcoded", "copied"}:
            continue
        candidates.append((key, entry))
    candidates.sort(key=lambda item: item[1].get("sort_key", [999, 9999, item[0].lower()]))
    return candidates[0] if candidates else None


def transcode_file(state, key, entry):
    output_path = Path(entry["output_path"])
    source_path = Path(entry["source_path"])
    temp_output = output_path.parent / f".{output_path.stem}.transcoding{output_path.suffix}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_output.exists():
        temp_output.unlink()

    entry["state"] = "transcoding"
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_error"] = ""
    entry["next_retry_at"] = None
    entry["retry_delay"] = 0
    set_current_activity(state, "transcoding", entry.get("display_name") or source_path.name)
    update_status(state)

    valid_source, source_reason = validate_video_file(source_path, check_stable=True)
    if not valid_source:
        entry["state"] = "failed"
        entry["last_error"] = f"invalid_source:{source_reason}"
        clear_current_activity(state, "failed")
        log(f"源文件无效，跳过转码: {source_path.name} ({source_reason})")
        update_status(state)
        return False

    log(f"开始转码: {entry.get('display_name') or source_path.name} → {output_path.name}")
    return_code = run_logged_process([
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-dn",
        "-sn",
        "-vf",
        f"scale=-2:{ACTIVE_PROFILE.get('resolution', 720)}",
        "-c:v",
        ACTIVE_PROFILE.get("videoCodec", "libx264"),
        "-preset",
        ACTIVE_PROFILE.get("preset", "fast"),
        "-b:v",
        ACTIVE_PROFILE.get("videoBitrate", "500k"),
        "-c:a",
        "aac",
        "-b:a",
        ACTIVE_PROFILE.get("audioBitrate", "64k"),
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-metadata",
        "title=",
        "-metadata",
        "comment=",
        "-metadata",
        "description=",
        "-metadata",
        "synopsis=",
        "-metadata",
        "show=",
        "-metadata",
        "episode_id=",
        "-metadata",
        "network=",
        "-metadata",
        "artist=",
        "-metadata",
        "album=",
        "-metadata",
        "genre=",
        "-metadata",
        "date=",
        "-metadata",
        "creation_time=",
        "-movflags",
        "+faststart",
        str(temp_output),
    ], "transcode", entry.get("display_name") or source_path.name)

    if STOP_REQUESTED:
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        entry["state"] = "seen"
        entry["last_error"] = "stopped_during_transcode"
        clear_current_activity(state, "stopping")
        log(f"转码已中止: {source_path.name}")
        update_status(state)
        return False

    # Validate transcode output with stability check (ensure ffmpeg has fully flushed to disk)
    valid_output, output_info = validate_video_file(temp_output, check_stable=True)
    if return_code != 0 or not temp_output.exists() or not valid_output:
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        reason = f"ffmpeg_failed:{return_code}" if return_code != 0 else f"invalid_output:{output_info}"
        scheduled = schedule_retry(entry, reason)
        clear_current_activity(state, "retry_wait" if scheduled else "failed")
        log(f"转码失败: {source_path.name} ({reason})")
        update_status(state)
        return False

    # Additional integrity check: verify the transcoded file's duration is reasonable
    # compared to the source (should be at least 80% of source duration)
    source_info = probe_video_info(source_path)
    if source_info and source_info["duration"] > 0 and isinstance(output_info, dict):
        output_duration = output_info.get("duration", 0)
        if output_duration < source_info["duration"] * 0.8:
            log(f"转码输出时长异常: 源={int(source_info['duration'])}s 输出={int(output_duration)}s，可能截断")
            temp_output.unlink(missing_ok=True)
            reason = f"output_duration_truncated:src={int(source_info['duration'])}s,out={int(output_duration)}s"
            scheduled = schedule_retry(entry, reason)
            clear_current_activity(state, "retry_wait" if scheduled else "failed")
            update_status(state)
            return False

    os.replace(temp_output, output_path)
    entry["state"] = "transcoded"
    entry["last_error"] = ""
    entry["next_retry_at"] = None
    entry["retry_delay"] = 0
    clear_current_activity(state, "idle")
    log(f"转码完成: {output_path.name}")
    update_status(state)
    return True


def copy_with_verification(state, source_path, target_path):
    assert_no_nas_overwrite(source_path, target_path)
    temp_target = target_path.parent / f".{target_path.name}.partial"
    if temp_target.exists():
        temp_target.unlink()

    target_path.parent.mkdir(parents=True, exist_ok=True)
    set_current_activity(state, "transferring", target_path.name)
    update_status(state)
    try:
        with open(source_path, "rb") as src, open(temp_target, "wb") as dst:
            while True:
                if STOP_REQUESTED:
                    raise InterruptedError("copy interrupted")
                chunk = src.read(COPY_BUFFER_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source_path, temp_target)
        if temp_target.stat().st_size != source_path.stat().st_size:
            raise OSError("size mismatch after copy")
        valid_temp_target, temp_target_reason = validate_video_file(temp_target)
        if not valid_temp_target:
            raise OSError(f"invalid target after copy: {temp_target_reason}")
        os.replace(temp_target, target_path)
        if target_path.stat().st_size != source_path.stat().st_size:
            raise OSError("size mismatch after rename")
        # Final validation of the file at its permanent NAS path
        valid_final, final_reason = validate_video_file(target_path)
        if not valid_final:
            # Remove the invalid file so it can be retried
            try:
                target_path.unlink()
            except OSError:
                pass
            raise OSError(f"invalid file at final path: {final_reason}")
    finally:
        clear_current_activity(state, "idle")
        if temp_target.exists() and STOP_REQUESTED:
            temp_target.unlink(missing_ok=True)


def try_mount_nas():
    """Attempt to mount the NAS if NAS_ROOT is not accessible."""
    if NAS_ROOT.exists():
        return True
    if not NAS_AUTO_MOUNT:
        log(f"NAS 未挂载，且已禁用自动挂载: {NAS_ROOT}")
        return False

    nas_mount_point = NAS_ROOT.parent
    configured_mount_url = NAS_MOUNT_URL.strip()

    if not nas_mount_point.exists() or not os.path.ismount(nas_mount_point):
        log(f"NAS 未挂载: {nas_mount_point}，尝试自动挂载...")
        try:
            nas_url = configured_mount_url
            if not nas_url:
                result = subprocess.run(
                    ["defaults", "read", "com.apple.finder", "FXConnectToLastURL"],
                    capture_output=True, text=True, timeout=5
                )
                nas_url = result.stdout.strip() if result.returncode == 0 else ""
            if nas_url.startswith("smb://"):
                log(f"发现 NAS 地址: {nas_url}，尝试挂载...")
                subprocess.run(
                    ["open", nas_url],
                    capture_output=True, text=True, timeout=10
                )
                for _ in range(15):
                    time.sleep(2)
                    if nas_mount_point.exists() and os.path.ismount(nas_mount_point):
                        log(f"NAS 已挂载: {nas_mount_point}")
                        return True
                log(f"NAS 挂载超时，请手动连接: {nas_url}")
            else:
                log("未找到可用 NAS 挂载地址")
        except Exception as exc:
            log(f"NAS 自动挂载失败: {exc}")
    return False


def sync_outputs_to_nas(state):
    if not NAS_ROOT.exists():
        raise RuntimeError(f"目标磁盘不可用，请先挂载或修正 targetDir: {NAS_ROOT}")

    NAS_SHOW_DIR.mkdir(parents=True, exist_ok=True)
    state["phase"] = "transferring"
    update_status(state)

    for output_path in sorted(FINAL_DIR.glob("*.mp4")):
        # Validate local mp4 before copying to NAS — skip invalid/incomplete files
        valid_local, local_reason = validate_video_file(output_path, check_stable=True)
        if not valid_local:
            log(f"跳过无效本地文件，不转移: {output_path.name} ({local_reason})")
            continue

        target_path = nas_path_for(output_path)
        if not same_size(output_path, target_path):
            assert_no_nas_overwrite(output_path, target_path)
            log(f"转移到目标磁盘: {output_path.name}")
            copy_with_verification(state, output_path, target_path)
            log(f"目标文件已验证: {target_path.name}")

        for entry in state["files"].values():
            if entry.get("output_path") == str(output_path):
                entry["nas_path"] = str(target_path)
                entry["state"] = "copied"
        refresh_counts(state)
        update_status(state)

def cleanup_tmp_dir(state):
    if FINAL_DIR.exists() and not STOP_REQUESTED:
        shutil.rmtree(FINAL_DIR)
        log(f"已清理本地转换目录: {FINAL_DIR}")
        try:
            FINAL_DIR.parent.rmdir()
        except OSError:
            pass
    if TMP_DIR.exists() and not STOP_REQUESTED:
        shutil.rmtree(TMP_DIR)
        log(f"已清理临时目录: {TMP_DIR}")
    if SHOW_STATE_DIR.exists() and not STOP_REQUESTED:
        shutil.rmtree(SHOW_STATE_DIR)
        log(f"已清理运行状态目录: {SHOW_STATE_DIR}")
    refresh_counts(state)
    update_status(state)


def snapshot_download_tree(source_files):
    total_size = sum(path.stat().st_size for path in source_files if path.exists())
    latest_mtime = max((int(path.stat().st_mtime) for path in source_files if path.exists()), default=0)
    return (len(source_files), total_size, latest_mtime)


def scan_existing_local_outputs(state):
    adopt_existing_local_show_dir()
    if not FINAL_DIR.exists():
        raise RuntimeError(f"未找到本地已完成目录: {FINAL_DIR}")

    state["files"] = {}
    matched = 0
    for output_path in sorted(FINAL_DIR.glob("*.mp4")):
        season, episode = extract_episode_token(output_path.name)
        key = output_path.name
        entry = state["files"].setdefault(key, {
            "attempts": 0,
            "stable_polls": STABLE_POLLS,
            "last_error": "",
        })
        entry["source_path"] = str(output_path)
        entry["partial_path"] = ""
        entry["output_path"] = str(output_path)
        entry["nas_path"] = str(nas_path_for(output_path))
        entry["size"] = output_path.stat().st_size
        entry["mtime"] = int(output_path.stat().st_mtime)
        entry["source_signature"] = f"local:{entry['size']}:{entry['mtime']}"
        entry["partial"] = False
        entry["ignored_small"] = False
        entry["season"] = season
        entry["episode"] = episode
        entry["episode_label"] = f"S{season:02d}E{episode:02d}" if season is not None and episode is not None else ""
        entry["display_name"] = output_path.name
        entry["sort_key"] = [season if season is not None else 999, episode if episode is not None else 9999, output_path.name.lower()]
        entry["state"] = "transcoded"
        matched += 1

    refresh_counts(state)
    if matched == 0:
        raise RuntimeError(f"本地目录下未发现 mp4: {FINAL_DIR}")


def wait_for_download_and_transcode(state):
    state["phase"] = "downloading"
    set_current_activity(state, "starting_transmission", SHOW_NAME)
    update_status(state)

    log(f"=== 开始下载: {SHOW_NAME} ===")
    notify(f"《{SHOW_NAME}》下载开始")

    try:
        process = subprocess.Popen(
            [
                "transmission-cli",
                "-g", str(TRANSMISSION_CONFIG_DIR),
                "-u", str(TRANSMISSION_UPLOAD_LIMIT_KBPS),
                "-w", str(TMP_DIR),
                MAGNET,
            ],
            stdout=open(LOG_FILE, "a", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
            env=COMMAND_ENV,
        )
    except FileNotFoundError:
        raise RuntimeError("未找到 transmission-cli")

    state["transmission_pid"] = process.pid
    set_current_activity(state, "downloading", SHOW_NAME)
    update_status(state)

    start_time = time.time()
    last_hour_notice = 0
    settle_polls = 0
    previous_snapshot = None

    while True:
        if STOP_REQUESTED:
            log("收到停止请求，终止下载进程")
            process.terminate()
            break

        if time.time() - start_time >= TIMEOUT_HOURS * 3600:
            log("下载超时，终止 transmission")
            process.terminate()
            state["phase"] = "timeout"
            state["last_error"] = "download_timeout"
            set_current_activity(state, "timeout", SHOW_NAME)
            update_status(state)
            notify(f"《{SHOW_NAME}》下载超时，已停止")
            break

        source_files = scan_downloads(state)
        current_hour = int((time.time() - start_time) / 3600)
        if current_hour > last_hour_notice:
            notify(f"《{SHOW_NAME}》下载中，已发现 {len(source_files)} 个视频")
            last_hour_notice = current_hour

        candidate = select_transcode_candidate(state)
        if candidate:
            state["phase"] = "transcoding"
            update_status(state)
            transcode_file(state, candidate[0], candidate[1])
            source_files = scan_downloads(state)
        else:
            set_current_activity(state, "downloading", SHOW_NAME)

        if process.poll() is not None:
            return_code = process.returncode
            partial_exists = any_partial_files(TMP_DIR)
            payload_exists = has_download_payload(TMP_DIR)
            if not source_files:
                state["transmission_pid"] = None
                state["download_complete"] = False
                state["last_error"] = f"transmission_exited_without_video_payload(code={return_code}, payload={payload_exists}, partial={partial_exists})"
                set_current_activity(state, "download_failed", SHOW_NAME)
                update_status(state)
                raise RuntimeError(f"transmission 提前退出，未拿到可转码视频 (code={return_code})")
            log(f"transmission 已退出，进入收尾检查: code={return_code} payload={payload_exists} partial={partial_exists}")
            state["download_complete"] = True
            break

        partial_exists = any_partial_files(TMP_DIR)
        if has_download_payload(TMP_DIR) and transmission_is_seeding():
            if SEED_IDLE_SECONDS > 0:
                log(f"检测到 transmission 已开始做种，继续保留 {SEED_IDLE_SECONDS} 秒")
                seed_deadline = time.time() + SEED_IDLE_SECONDS
                while time.time() < seed_deadline and not STOP_REQUESTED and process.poll() is None:
                    time.sleep(min(5, max(1, int(seed_deadline - time.time()))))
                if STOP_REQUESTED:
                    log("做种等待期间收到停止请求，终止下载进程")
                    process.terminate()
                    break
            else:
                log("检测到 transmission 已开始做种，停止下载阶段")
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            state["download_complete"] = True
            break

        snapshot = snapshot_download_tree(source_files)
        if source_files and not partial_exists:
            if snapshot == previous_snapshot:
                settle_polls += 1
            else:
                settle_polls = 1
            previous_snapshot = snapshot
        else:
            settle_polls = 0
            previous_snapshot = snapshot

        if source_files and not partial_exists and settle_polls >= DOWNLOAD_SETTLE_POLLS:
            log("检测到所有下载文件稳定，停止 transmission 做种")
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            state["download_complete"] = True
            break

        update_status(state)
        time.sleep(POLL_INTERVAL)

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

    state["download_complete"] = True
    state["transmission_pid"] = None
    set_current_activity(state, "finishing_transcodes", SHOW_NAME)
    update_status(state)

    # Clean up any leftover .transcoding temp files from previous interrupted runs
    if FINAL_DIR.exists():
        for leftover in FINAL_DIR.glob(".*.transcoding.mp4"):
            log(f"清理上次中断的转码临时文件: {leftover.name}")
            leftover.unlink(missing_ok=True)

    while not STOP_REQUESTED:
        source_files = scan_downloads(state)
        candidate = select_transcode_candidate(state)
        if not candidate:
            break
        state["phase"] = "transcoding"
        update_status(state)
        transcode_file(state, candidate[0], candidate[1])

    refresh_counts(state)
    clear_current_activity(state, "idle")
    update_status(state)


def drain_remaining_transcodes(state):
    idle_polls = 0

    while not STOP_REQUESTED:
        source_files = scan_downloads(state)
        candidate = select_transcode_candidate(state)
        if candidate:
            idle_polls = 0
            state["phase"] = "transcoding"
            update_status(state)
            transcode_file(state, candidate[0], candidate[1])
            continue

        pending = pending_transcode_entries(state)
        if not pending:
            break

        partial_waiting = any(entry.get("state") == "partial_only" for entry in pending)
        retry_deadline = next_retry_deadline(state)
        if retry_deadline:
            sleep_seconds = max(1, min(POLL_INTERVAL, int(retry_deadline - time.time()) if retry_deadline > time.time() else 1))
            set_current_activity(state, "waiting_retry", SHOW_NAME)
        elif partial_waiting or source_files:
            sleep_seconds = POLL_INTERVAL
            set_current_activity(state, "waiting_finalize", SHOW_NAME)
        else:
            idle_polls += 1
            if idle_polls >= DOWNLOAD_SETTLE_POLLS:
                break
            sleep_seconds = POLL_INTERVAL
            set_current_activity(state, "waiting_finalize", SHOW_NAME)

        update_status(state)
        time.sleep(sleep_seconds)

    refresh_counts(state)
    clear_current_activity(state, "idle")
    update_status(state)


def signal_handler(sig, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log(f"收到信号 {sig}，准备安全停止")
    terminate_active_process(grace_seconds=5)


def main():
    if not SHOW_NAME:
        print("Usage: python3 media-downloader.py [剧名] [磁力链接]\n       python3 media-downloader.py [剧名]")
        sys.exit(1)

    state = None
    try:
        configure_runtime_paths()
        acquire_show_lock()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        state = build_state()
        prepare_run_environment(state)
        prepare_show_metadata(state)

        FINAL_DIR.parent.mkdir(parents=True, exist_ok=True)
        if START_MODE == "adopt":
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            FINAL_DIR.mkdir(parents=True, exist_ok=True)
            adopt_phase(state)
        elif START_MODE == "download":
            ensure_transmission_cli_only()
            handle_stale_downloads_before_start()
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            FINAL_DIR.mkdir(parents=True, exist_ok=True)
            TRANSMISSION_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            write_download_meta()

        set_current_activity(state, "starting", SHOW_NAME)
        update_status(state)

        if START_MODE == "download":
            wait_for_download_and_transcode(state)
        elif START_MODE == "adopt":
            state["phase"] = "transcoding"
            set_current_activity(state, "organizing_adopted", SHOW_NAME)
            update_status(state)
        else:
            state["phase"] = "organizing"
            set_current_activity(state, "scanning_local_outputs", CANONICAL_SHOW_NAME)
            update_status(state)
            scan_existing_local_outputs(state)
            state["download_complete"] = True
            clear_current_activity(state, "idle")
            update_status(state)

        if STOP_REQUESTED:
            state["phase"] = "stopped"
            state["stop_requested"] = True
            clear_current_activity(state, "stopped")
            update_status(state)
            notify(f"《{SHOW_NAME}》已停止")
            return

        state["phase"] = "syncing"
        set_current_activity(state, "verifying_transcodes", SHOW_NAME)
        update_status(state)
        drain_remaining_transcodes(state)
        ensure_transcodes_complete(state)

        if STOP_REQUESTED:
            state["phase"] = "stopped"
            state["stop_requested"] = True
            clear_current_activity(state, "stopped")
            update_status(state)
            notify(f"《{SHOW_NAME}》已停止")
            return

        state["phase"] = "transferring"
        set_current_activity(state, "preparing_transfer", SHOW_NAME)
        update_status(state)
        sync_outputs_to_nas(state)
        ensure_nas_sync_complete()

        if STOP_REQUESTED:
            state["phase"] = "stopped"
            state["stop_requested"] = True
            clear_current_activity(state, "stopped")
            update_status(state)
            notify(f"《{SHOW_NAME}》已停止")
            return

        cleanup_tmp_dir(state)

        refresh_counts(state)
        state["phase"] = "done"
        state["stop_requested"] = False
        state["last_error"] = ""
        clear_current_activity(state, "done")
        update_status(state)
        notify(f"《{SHOW_NAME}》全部处理完成")
        log(f"=== 完成: {SHOW_NAME} ===")
        clear_completed_status_entry(state)
    except Exception as exc:
        if state is not None:
            state["phase"] = "failed"
            state["last_error"] = str(exc)
            clear_current_activity(state, "failed")
            update_status(state)
        notify(f"《{SHOW_NAME}》处理失败")
        log(f"流水线失败: {exc}")
        raise
    finally:
        cleanup_lingering_processes()
        log_lingering_processes()
        release_show_lock()


if __name__ == "__main__":
    main()
