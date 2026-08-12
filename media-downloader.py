#!/usr/bin/env python3
"""Agent-friendly media ingest pipeline using only the Python standard library."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SKILL_DIR / ".runtime"
DEFAULT_CONFIG_FILE = SKILL_DIR / "config.json"
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".rm", ".rmvb", ".3gp"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ACTIVE_CHILD: subprocess.Popen | None = None
STOP_REQUESTED = False


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"JSON 文件无效: {path}: {exc}") from exc


def atomic_json(path: Path, data, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if private:
        os.chmod(temp, 0o600)
    os.replace(temp, path)


def ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def config_file() -> Path:
    return Path(os.environ.get("MEDIA_DOWNLOADER_CONFIG", DEFAULT_CONFIG_FILE)).expanduser().resolve()


def validate_config(data: dict) -> None:
    profiles = data.get("profiles")
    targets = data.get("targets")
    naming = data.get("namingPresets")
    if not isinstance(profiles, dict) or not profiles:
        raise RuntimeError("配置缺少 profiles")
    if not isinstance(targets, dict) or not targets:
        raise RuntimeError("配置缺少 targets")
    if not isinstance(naming, dict) or not naming:
        raise RuntimeError("配置缺少 namingPresets")
    defaults = data.get("defaultProfiles", {})
    if not isinstance(defaults, dict):
        raise RuntimeError("defaultProfiles 必须是对象")
    for media_type in ("tv", "movie"):
        profile_name = defaults.get(media_type)
        profile = profiles.get(profile_name)
        if not isinstance(profile, dict) or profile.get("type") != media_type:
            raise RuntimeError(f"defaultProfiles.{media_type} 未指向有效的 {media_type} 预设")
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or profile.get("type") not in {"tv", "movie"}:
            raise RuntimeError(f"profile 无效: {name}")
        target = profile.get("target")
        if target and target not in targets:
            raise RuntimeError(f"profile {name} 引用了未知 target: {target}")
        naming_name = profile.get("naming") or data.get("defaultNaming") or "plex"
        if naming_name not in naming:
            raise RuntimeError(f"profile {name} 引用了未知 naming: {naming_name}")
    for name, value in targets.items():
        raw = value.get("path") if isinstance(value, dict) else value
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(f"target 路径无效: {name}")


def load_config() -> dict:
    if not config_file().is_file():
        raise RuntimeError(f"配置不存在: {config_file()}；请复制 config.example.json 为 config.json")
    data = read_json(config_file())
    if not isinstance(data, dict):
        raise RuntimeError("配置根节点必须是对象")
    validate_config(data)
    return data


def runtime_file(env_name: str, filename: str) -> Path:
    return Path(os.environ.get(env_name, RUNTIME_DIR / filename)).expanduser().resolve()


def status_file() -> Path:
    return runtime_file("MEDIA_DOWNLOADER_STATUS_FILE", "status.json")


def candidate_file() -> Path:
    return runtime_file("MEDIA_DOWNLOADER_CANDIDATE_FILE", "candidates.json")


@contextlib.contextmanager
def json_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def status_update(task_id: str, **fields) -> dict:
    path = status_file()
    with json_lock(path):
        states = read_json(path, {})
        state = states.get(task_id, {})
        state.update(fields)
        state["updatedAt"] = now()
        states[task_id] = state
        atomic_json(path, states, private=True)
    return state


def status_read() -> dict:
    path = status_file()
    with json_lock(path):
        return read_json(path, {})


def sanitize_component(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value or value in {".", ".."}:
        raise ValueError("名称清理后为空")
    return value[:180]


def validate_title(title: str) -> str:
    if not title or title.strip() in {".", ".."}:
        raise ValueError("请提供有效标题")
    if len(title) > 200 or any(ord(char) < 32 for char in title) or "/" in title or "\\" in title:
        raise ValueError("标题包含不安全字符")
    return sanitize_component(title)


def resolve_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve(strict=False)


def contains_path(parent: Path, child: Path) -> bool:
    return child == parent or child.is_relative_to(parent)


def paths_overlap(left: Path, right: Path) -> bool:
    return contains_path(left, right) or contains_path(right, left)


def require_target_root(path: Path) -> None:
    forbidden = {Path("/"), Path.home().resolve(), Path("/Volumes")}
    if path in forbidden:
        raise RuntimeError(f"目标目录范围过大，拒绝使用: {path}")
    if not path.is_dir():
        raise RuntimeError(f"目标预设目录不可用: {path}")
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"目标预设目录不可写: {path}")
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        volume_root = Path("/Volumes") / parts[2]
        if not os.path.ismount(volume_root):
            raise RuntimeError(f"目标磁盘未挂载: {volume_root}")


def canonical_name(title: str, year: int | None) -> str:
    title = sanitize_component(title)
    if year and not re.search(rf"\({year}\)$", title):
        return f"{title} ({year})"
    return title


def task_id(media_type: str, title: str, year: int | None) -> str:
    digest = hashlib.sha256(f"{media_type}\0{title}\0{year or ''}".encode()).hexdigest()[:12]
    return f"{media_type}-{digest}"


def default_profile_name(config: dict, media_type: str) -> str:
    defaults = config.get("defaultProfiles", {})
    if isinstance(defaults, dict) and defaults.get(media_type):
        return str(defaults[media_type])
    return str(config.get("defaultProfile") or ("tv1080" if media_type == "tv" else "movie1080"))


def select_profile(config: dict, media_type: str, name: str | None) -> tuple[str, dict]:
    profiles = config.get("profiles", {})
    name = name or default_profile_name(config, media_type)
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise RuntimeError(f"转码预设不存在: {name}")
    if profile.get("type", media_type) != media_type:
        raise RuntimeError(f"预设 {name} 不适用于 {media_type}")
    container = str(profile.get("container", "mp4")).lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{2,8}", container):
        raise RuntimeError(f"预设容器无效: {container}")
    return name, {**profile, "container": container}


def select_target(config: dict, profile: dict, requested: str | None) -> tuple[str, Path]:
    env_target = os.environ.get("MEDIA_DOWNLOADER_TARGET_DIR")
    if env_target:
        return "environment", resolve_path(env_target)
    targets = config.get("targets", {})
    name = requested or profile.get("target")
    if name:
        raw = targets.get(name) if isinstance(targets, dict) else None
        if isinstance(raw, dict):
            raw = raw.get("path")
        if not raw:
            raise RuntimeError(f"目标预设不存在: {name}")
        return str(name), resolve_path(raw)
    raw = profile.get("targetDir") or config.get("targetDir")
    if not raw:
        raise RuntimeError("未配置目标预设")
    return "legacy", resolve_path(raw)


def select_naming(config: dict, profile: dict, media_type: str, requested: str | None) -> tuple[str, dict]:
    name = requested or profile.get("naming") or config.get("defaultNaming") or "plex"
    presets = config.get("namingPresets", {})
    preset = presets.get(name) if isinstance(presets, dict) else None
    settings = preset.get(media_type) if isinstance(preset, dict) else None
    if not isinstance(settings, dict):
        raise RuntimeError(f"命名预设不存在或不支持 {media_type}: {name}")
    required = {"showDir", "episodeFile", "seasonDir"} if media_type == "tv" else {"showDir", "movieFile"}
    missing = sorted(required - settings.keys())
    if missing:
        raise RuntimeError(f"命名预设 {name} 缺少: {', '.join(missing)}")
    return str(name), settings


def render_path(template: str, fields: dict) -> Path:
    try:
        rendered = str(template).format_map(fields)
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"命名模板无效: {template}: {exc}") from exc
    raw = Path(rendered)
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise RuntimeError(f"命名模板产生不安全路径: {rendered}")
    return Path(*(sanitize_component(part) for part in raw.parts))


def metadata_from_file(path: str | None) -> dict:
    if not path:
        return {}
    data = read_json(resolve_path(path))
    if not isinstance(data, dict):
        raise RuntimeError("metadata JSON 必须是对象")
    return data


def http_json(url: str, params: dict, headers: dict | None = None, timeout: int = 20):
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "media-downloader/2.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc} 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc} 连接失败: {exc.reason}") from exc


def fetch_tmdb(config: dict, media_type: str, title: str, year: int | None) -> dict:
    metadata_config = config.get("metadata", {})
    key_env = metadata_config.get("apiKeyEnv", "TMDB_API_KEY")
    api_key = os.environ.get(str(key_env), "")
    if not api_key:
        return {}
    language = metadata_config.get("language", "zh-CN")
    kind = "tv" if media_type == "tv" else "movie"
    params = {"api_key": api_key, "language": language, "query": title}
    params["first_air_date_year" if kind == "tv" else "year"] = year
    search = http_json(f"https://api.themoviedb.org/3/search/{kind}", params)
    results = search.get("results", []) if isinstance(search, dict) else []
    if not results:
        return {}
    item = results[0]
    tmdb_id = item.get("id")
    detail = http_json(
        f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
        {"api_key": api_key, "language": language, "append_to_response": "external_ids"},
    )
    date_key = "first_air_date" if kind == "tv" else "release_date"
    name_key = "name" if kind == "tv" else "title"
    original_key = "original_name" if kind == "tv" else "original_title"
    date = detail.get(date_key) or item.get(date_key) or ""
    externals = detail.get("external_ids", {}) if isinstance(detail.get("external_ids"), dict) else {}
    return {
        "title": detail.get(name_key) or item.get(name_key),
        "originalTitle": detail.get(original_key) or item.get(original_key),
        "year": int(date[:4]) if str(date)[:4].isdigit() else None,
        "premiered": date,
        "plot": detail.get("overview") or item.get("overview") or "",
        "genres": [genre.get("name") for genre in detail.get("genres", []) if genre.get("name")],
        "studio": next((company.get("name") for company in detail.get("production_companies", []) if company.get("name")), ""),
        "ids": {"tmdb": tmdb_id, "imdb": externals.get("imdb_id"), "tvdb": externals.get("tvdb_id")},
        "posterUrl": f"https://image.tmdb.org/t/p/original{detail.get('poster_path')}" if detail.get("poster_path") else "",
        "fanartUrl": f"https://image.tmdb.org/t/p/original{detail.get('backdrop_path')}" if detail.get("backdrop_path") else "",
    }


def fetch_tvmaze(title: str) -> dict:
    payload = http_json("https://api.tvmaze.com/search/shows", {"q": title})
    candidates = [item.get("show") for item in payload if isinstance(item, dict) and isinstance(item.get("show"), dict)] if isinstance(payload, list) else []
    if not candidates:
        return {}
    normalized = re.sub(r"\W", "", title).casefold()
    show = max(candidates, key=lambda item: int(re.sub(r"\W", "", str(item.get("name", ""))).casefold() == normalized) * 100 + int(bool(item.get("image"))))
    premiered = show.get("premiered") or ""
    image = show.get("image") if isinstance(show.get("image"), dict) else {}
    externals = show.get("externals") if isinstance(show.get("externals"), dict) else {}
    summary = re.sub(r"<[^>]+>", "", show.get("summary") or "")
    return {
        "title": show.get("name"),
        "originalTitle": show.get("name"),
        "year": int(premiered[:4]) if premiered[:4].isdigit() else None,
        "premiered": premiered,
        "plot": summary,
        "genres": show.get("genres", []),
        "studio": (show.get("network") or show.get("webChannel") or {}).get("name", ""),
        "ids": {"tvmaze": show.get("id"), "imdb": externals.get("imdb"), "tvdb": externals.get("thetvdb")},
        "posterUrl": image.get("original") or image.get("medium") or "",
    }


def resolve_metadata(config: dict, args) -> dict:
    supplied = metadata_from_file(args.metadata)
    fetched = {}
    offline = bool(args.offline or os.environ.get("MEDIA_DOWNLOADER_OFFLINE") == "1")
    metadata_config = config.get("metadata", {})
    if not offline and metadata_config.get("provider") == "tmdb":
        try:
            fetched = fetch_tmdb(config, args.media_type, args.title, args.year)
        except Exception as exc:
            print(f"警告: TMDB 元数据查询失败: {exc}", file=sys.stderr)
    if not fetched and not offline and args.media_type == "tv" and metadata_config.get("tvFallback", "tvmaze") == "tvmaze":
        try:
            fetched = fetch_tvmaze(args.title)
        except Exception as exc:
            print(f"警告: TVMaze 元数据查询失败: {exc}", file=sys.stderr)
    metadata = {**fetched, **supplied}
    metadata["title"] = args.title
    metadata["year"] = args.year or metadata.get("year")
    metadata.setdefault("originalTitle", args.title)
    metadata.setdefault("plot", "")
    metadata.setdefault("premiered", f"{metadata['year']}-01-01" if metadata.get("year") else "")
    metadata.setdefault("genres", [])
    metadata.setdefault("studio", "")
    metadata.setdefault("ids", {})
    return metadata


def build_context(config: dict, args) -> dict:
    title = validate_title(args.title)
    profile_name, profile = select_profile(config, args.media_type, args.profile)
    metadata = resolve_metadata(config, args)
    canonical = canonical_name(metadata.get("title") or title, metadata.get("year"))
    target_name, target_root = select_target(config, profile, args.target)
    naming_name, naming = select_naming(config, profile, args.media_type, args.naming)
    base_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_BASE_DIR") or config.get("baseDir") or (SKILL_DIR / "work"))
    state_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_STATE_DIR") or config.get("stateDir") or (base_root / ".state"))
    require_target_root(target_root)
    if base_root in {Path("/"), Path.home().resolve(), Path("/Volumes")}:
        raise RuntimeError(f"工作目录范围过大，拒绝使用: {base_root}")
    if paths_overlap(base_root, target_root):
        raise RuntimeError(f"工作区与目标目录不得重叠: {base_root} / {target_root}")
    if paths_overlap(state_root, target_root):
        raise RuntimeError(f"状态目录与目标目录不得重叠: {state_root} / {target_root}")
    identifier = task_id(args.media_type, title, metadata.get("year"))
    work_parent = base_root / ".media-downloader-work"
    work_root = work_parent / identifier
    if not contains_path(work_parent.resolve(strict=False), work_root.resolve(strict=False)):
        raise RuntimeError("任务工作目录逃逸")
    naming_fields = {
        "title": sanitize_component(metadata.get("title") or title),
        "canonical": canonical,
        "year": metadata.get("year") or "",
        "ext": profile["container"],
    }
    target_show = target_root / render_path(naming["showDir"], naming_fields)
    if not contains_path(target_root, target_show):
        raise RuntimeError("目标目录逃逸")
    return {
        "id": identifier,
        "title": title,
        "canonical": canonical,
        "mediaType": args.media_type,
        "profileName": profile_name,
        "profile": profile,
        "targetName": target_name,
        "namingName": naming_name,
        "naming": naming,
        "namingFields": naming_fields,
        "targetRoot": target_root,
        "targetShow": target_show,
        "baseRoot": base_root,
        "stateRoot": state_root,
        "workRoot": work_root,
        "sourceRoot": work_root / "source",
        "outputRoot": work_root / "output",
        "marker": work_root / ".media-downloader-owned.json",
        "logPath": Path(f"/tmp/media-downloader-{identifier}.log"),
        "metadata": metadata,
        "args": args,
        "config": config,
    }


def source_fingerprint(ctx: dict, source: str) -> str:
    value = f"{source}\0{ctx['profileName']}\0{ctx['namingName']}\0{ctx['canonical']}"
    return hashlib.sha256(value.encode()).hexdigest()


def remove_owned_work(ctx: dict) -> None:
    work = ctx["workRoot"].resolve()
    parent = (ctx["baseRoot"] / ".media-downloader-work").resolve()
    marker = ctx["marker"]
    ownership = read_json(marker, {}) if marker.exists() else {}
    if not work.is_relative_to(parent) or work == parent or ownership.get("taskId") != ctx["id"]:
        raise RuntimeError(f"拒绝清理未验证目录: {work}")
    shutil.rmtree(work)


def ensure_work(ctx: dict, source: str) -> None:
    work = ctx["workRoot"]
    marker = ctx["marker"]
    fingerprint = source_fingerprint(ctx, source)
    if work.exists() and ctx["args"].reset_work:
        remove_owned_work(ctx)
    if work.exists():
        existing = read_json(marker, {}) if marker.exists() else {}
        if existing.get("taskId") != ctx["id"]:
            raise RuntimeError(f"拒绝接管无所有权标记的工作目录: {work}")
        if existing.get("sourceFingerprint") != fingerprint:
            raise RuntimeError("失败任务的来源已变化；确认后使用 --reset-work 重建工作区")
    else:
        work.mkdir(parents=True)
        atomic_json(marker, {"taskId": ctx["id"], "title": ctx["title"], "sourceFingerprint": fingerprint, "createdAt": now()})
    ctx["sourceRoot"].mkdir(parents=True, exist_ok=True)
    ctx["outputRoot"].mkdir(parents=True, exist_ok=True)
    ctx["stateRoot"].mkdir(parents=True, exist_ok=True)
    ensure_private_file(ctx["logPath"])


@contextlib.contextmanager
def task_lock(ctx: dict):
    path = ctx["stateRoot"] / f"{ctx['id']}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"任务已在运行: {ctx['title']}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def log(ctx: dict, message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    ensure_private_file(ctx["logPath"])
    with open(ctx["logPath"], "a", encoding="utf-8", errors="replace") as handle:
        handle.write(line + "\n")


def redacted_source(source: str) -> str:
    if source.startswith("magnet:"):
        match = re.search(r"btih:([A-Za-z0-9]+)", source)
        return f"magnet:{match.group(1)[:12] if match else 'provided'}"
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return str(Path(source).expanduser())


def validate_source(source: str, allow_local: bool = True) -> str:
    if not source or source != source.strip() or any(ord(char) < 32 for char in source):
        raise RuntimeError("来源包含不安全控制字符")
    local = Path(source).expanduser()
    if allow_local and local.exists():
        return source
    if source.startswith("magnet:"):
        parsed = urllib.parse.urlsplit(source)
        if "xt=urn:btih:" not in parsed.query.casefold():
            raise RuntimeError("magnet 来源缺少 BTIH")
        return source
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("来源只允许本地路径、magnet 或 HTTP(S)")
    return source


def signal_handler(_signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    if ACTIVE_CHILD and ACTIVE_CHILD.poll() is None:
        ACTIVE_CHILD.terminate()


def scrub_log(path: Path, secrets: list[str]) -> None:
    replacements = [(value, redacted_source(value)) for value in secrets if value]
    if not replacements or not path.exists():
        return
    temp = path.with_name(f".{path.name}.{os.getpid()}.scrub")
    with open(path, "r", encoding="utf-8", errors="replace") as source, open(temp, "w", encoding="utf-8") as target:
        for line in source:
            for secret, replacement in replacements:
                line = line.replace(secret, replacement)
            target.write(line)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def run_child(ctx: dict, command: list[str], operation: str, timeout_seconds: int | None = None, redactions: list[str] | None = None) -> None:
    global ACTIVE_CHILD
    ensure_private_file(ctx["logPath"])
    status_update(ctx["id"], phase=operation, currentOperation=operation, childPid=None)
    code = -1
    try:
        with open(ctx["logPath"], "a", encoding="utf-8", errors="replace") as handle:
            ACTIVE_CHILD = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
            status_update(ctx["id"], childPid=ACTIVE_CHILD.pid)
            started = time.monotonic()
            while ACTIVE_CHILD.poll() is None:
                if STOP_REQUESTED:
                    ACTIVE_CHILD.terminate()
                    try:
                        ACTIVE_CHILD.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        ACTIVE_CHILD.kill()
                    raise InterruptedError("任务已停止")
                if timeout_seconds and time.monotonic() - started > timeout_seconds:
                    ACTIVE_CHILD.terminate()
                    try:
                        ACTIVE_CHILD.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        ACTIVE_CHILD.kill()
                    raise TimeoutError(f"{operation} 超时")
                time.sleep(0.25)
            code = ACTIVE_CHILD.returncode
    finally:
        ACTIVE_CHILD = None
        scrub_log(ctx["logPath"], redactions or [])
    status_update(ctx["id"], childPid=None)
    if code != 0:
        raise RuntimeError(f"{operation} 失败，退出码 {code}，日志: {ctx['logPath']}")


def is_safe_file(root: Path, path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())


def media_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTS and not root.is_symlink() else []
    return sorted(path for path in root.rglob("*") if is_safe_file(root, path) and path.suffix.lower() in VIDEO_EXTS)


def load_candidates() -> dict:
    path = candidate_file()
    with json_lock(path):
        return read_json(path, {})


def save_candidates(entries: list[dict]) -> None:
    path = candidate_file()
    cutoff = time.time() - 7 * 86400
    with json_lock(path):
        data = read_json(path, {})
        data = {key: value for key, value in data.items() if float(value.get("createdEpoch", 0)) >= cutoff}
        for entry in entries:
            data[entry["candidateId"]] = entry
        atomic_json(path, data, private=True)


def torznab_attr(item: ET.Element, name: str):
    for attr in item.findall(".//{*}attr"):
        if attr.attrib.get("name") == name:
            return attr.attrib.get("value")
    return None


def jackett_search(name: str, source: dict, query: str, media_type: str, limit: int) -> list[dict]:
    key_env = str(source.get("apiKeyEnv", "JACKETT_API_KEY"))
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"搜索源 {name} 缺少环境变量 {key_env}")
    base = str(source.get("url", "")).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError(f"搜索源 {name} URL 无效")
    indexer = urllib.parse.quote(str(source.get("indexer", "all")), safe="")
    url = f"{base}/api/v2.0/indexers/{indexer}/results/torznab/api"
    search_type = "tvsearch" if media_type == "tv" else "movie"
    params = {"apikey": api_key, "t": search_type, "q": query}
    if source.get("categories"):
        params["cat"] = ",".join(str(value) for value in source["categories"])
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "media-downloader/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=int(source.get("timeoutSeconds", 30))) as response:
            root = ET.fromstring(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Jackett {name} 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Jackett {name} 连接失败: {exc.reason}") from exc
    results = []
    for item in root.findall(".//item")[:limit]:
        enclosure = item.find("enclosure")
        download_url = enclosure.attrib.get("url") if enclosure is not None else ""
        download_url = download_url or item.findtext("link", "")
        if not download_url:
            continue
        try:
            validate_source(download_url, allow_local=False)
        except RuntimeError:
            continue
        title = item.findtext("title", "").strip()
        size_text = torznab_attr(item, "size") or (enclosure.attrib.get("length") if enclosure is not None else "0") or "0"
        seeders_text = torznab_attr(item, "seeders") or "0"
        identifier = hashlib.sha256(f"{name}\0{download_url}".encode()).hexdigest()[:16]
        results.append({
            "candidateId": identifier,
            "source": name,
            "title": title,
            "sizeBytes": int(size_text) if str(size_text).isdigit() else 0,
            "seeders": int(seeders_text) if str(seeders_text).isdigit() else 0,
            "published": item.findtext("pubDate", ""),
            "tracker": torznab_attr(item, "tracker") or "",
            "kind": "torrent",
            "downloadUrl": download_url,
            "createdAt": now(),
            "createdEpoch": time.time(),
        })
    return results


def command_search(args) -> int:
    config = load_config()
    sources = config.get("searchSources", {})
    if not isinstance(sources, dict):
        raise RuntimeError("searchSources 必须是对象")
    selected = args.source or [name for name, value in sources.items() if isinstance(value, dict) and value.get("enabled", True)]
    candidates: list[dict] = []
    browse: list[dict] = []
    errors: list[dict] = []
    limit = max(1, min(args.limit, 100))
    for name in selected:
        source = sources.get(name)
        if not isinstance(source, dict):
            errors.append({"source": name, "error": "搜索源不存在"})
            continue
        try:
            kind = source.get("type")
            if kind == "jackett":
                candidates.extend(jackett_search(name, source, args.query, args.media_type, limit))
            elif kind == "web":
                template = str(source.get("urlTemplate", ""))
                if not template:
                    raise RuntimeError("缺少 urlTemplate")
                browse_url = template.replace("{query}", urllib.parse.quote_plus(args.query))
                validate_source(browse_url, allow_local=False)
                browse.append({"source": name, "browseUrl": browse_url, "requiresAgent": True})
            else:
                raise RuntimeError(f"不支持的搜索源类型: {kind}")
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)})
    candidates.sort(key=lambda item: (-item["seeders"], item["title"].casefold()))
    candidates = candidates[:limit]
    save_candidates(candidates)
    public = [{key: value for key, value in item.items() if key not in {"downloadUrl", "createdEpoch"}} for item in candidates]
    print(json.dumps({"query": args.query, "mediaType": args.media_type, "candidates": public, "browse": browse, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if public or browse else 1


def resolve_source(args) -> tuple[str, str]:
    provided = int(bool(args.candidate)) + int(bool(args.source)) + int(bool(args.source_file))
    if provided != 1:
        raise RuntimeError("请且仅提供一个来源：位置参数、--source-file 或 --candidate")
    if args.candidate:
        entry = load_candidates().get(args.candidate)
        if not entry:
            raise RuntimeError(f"候选不存在或已过期: {args.candidate}")
        if float(entry.get("createdEpoch", 0)) < time.time() - 7 * 86400:
            raise RuntimeError(f"候选不存在或已过期: {args.candidate}")
        return validate_source(str(entry["downloadUrl"]), allow_local=False), "aria2"
    source = args.source
    if args.source_file:
        path = resolve_path(args.source_file)
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise RuntimeError("--source-file 必须存在且权限为 0600")
        source = path.read_text(encoding="utf-8").strip()
    return validate_source(source), args.downloader


def classify_downloader(source: str, requested: str) -> str:
    if requested != "auto":
        return requested
    local = Path(source).expanduser()
    if local.exists():
        return "aria2" if local.suffix.lower() == ".torrent" else "local"
    if source.startswith("magnet:"):
        return "aria2"
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"不支持的来源协议: {parsed.scheme or 'unknown'}")
    suffix = Path(parsed.path).suffix.lower()
    return "aria2" if suffix in VIDEO_EXTS | {".torrent"} else "yt-dlp"


def acquire(ctx: dict, source: str, requested: str) -> list[Path]:
    downloader = classify_downloader(source, requested)
    log(ctx, f"获取来源: {redacted_source(source)} downloader={downloader}")
    if downloader == "local":
        root = resolve_path(source)
        if not root.exists():
            raise RuntimeError(f"本地来源不存在: {root}")
        files = media_files(root)
    elif downloader == "aria2":
        if not shutil.which("aria2c"):
            raise RuntimeError("缺少 aria2c")
        command = [
            "aria2c", "--no-conf=true", f"--dir={ctx['sourceRoot']}", "--continue=true",
            "--auto-file-renaming=false", "--allow-overwrite=false", "--file-allocation=none",
            "--check-integrity=true", "--seed-time=0", "--summary-interval=10",
        ]
        input_file = None
        local_source = Path(source).expanduser()
        if local_source.exists():
            command.append(str(local_source.resolve()))
        else:
            input_file = ctx["workRoot"] / ".aria2-input.txt"
            input_file.write_text(source + "\n", encoding="utf-8")
            os.chmod(input_file, 0o600)
            command.append(f"--input-file={input_file}")
        try:
            run_child(ctx, command, "downloading", int(ctx["config"].get("timeoutHours", 24)) * 3600, [source])
        finally:
            if input_file:
                input_file.unlink(missing_ok=True)
        files = media_files(ctx["sourceRoot"])
    elif downloader == "yt-dlp":
        if not shutil.which("yt-dlp"):
            raise RuntimeError("网页媒体来源需要 yt-dlp；请先运行 doctor")
        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("yt-dlp 只接受 HTTP(S) URL")
        playlist_flag = "--yes-playlist" if ctx["args"].playlist else "--no-playlist"
        input_file = ctx["workRoot"] / ".yt-dlp-input.txt"
        input_file.write_text(source + "\n", encoding="utf-8")
        os.chmod(input_file, 0o600)
        command = [
            "yt-dlp", "--ignore-config", "--no-remote-components", playlist_flag, "--continue",
            "--no-overwrites", "--newline", "--write-info-json", "--write-thumbnail",
            "--paths", str(ctx["sourceRoot"]),
            "--output", "%(playlist_index|autonumber)03d %(title).180B [%(id)s].%(ext)s", "--batch-file", str(input_file),
        ]
        try:
            run_child(ctx, command, "downloading", int(ctx["config"].get("timeoutHours", 24)) * 3600, [source])
        finally:
            input_file.unlink(missing_ok=True)
        files = media_files(ctx["sourceRoot"])
    else:
        raise RuntimeError(f"不支持的下载器: {downloader}")
    if not files:
        raise RuntimeError("来源中未发现媒体文件")
    return files


EPISODE_PATTERNS = [
    re.compile(r"(?i)S(?P<season>\d{1,2})[ ._-]*E(?P<episode>\d{1,3})"),
    re.compile(r"(?i)(?P<season>\d{1,2})x(?P<episode>\d{1,3})"),
    re.compile(r"(?i)(?:^|[ ._\-\[])EP?(?P<episode>\d{1,3})(?:$|[ ._\-\]])"),
    re.compile(r"第\s*(?P<episode>\d{1,3})\s*集"),
]


def episode_from_name(path: Path, default_season: int) -> tuple[int, int] | None:
    for text in (path.stem, path.parent.name):
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(text)
            if match:
                season = int(match.groupdict().get("season") or default_season)
                return season, int(match.group("episode"))
    return None


def ffprobe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration,size", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 无法读取: {path.name}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {"duration": duration, "hasVideo": any(item.get("codec_type") == "video" for item in streams), "hasAudio": any(item.get("codec_type") == "audio" for item in streams)}


def validate_video(path: Path, minimum_duration: float) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"媒体文件为空: {path}")
    info = ffprobe(path)
    if not info["hasVideo"]:
        raise RuntimeError(f"媒体缺少视频流: {path.name}")
    if info["duration"] < minimum_duration:
        raise RuntimeError(f"媒体时长过短: {path.name} ({info['duration']:.1f}s)")
    return info


def planned_outputs(ctx: dict, sources: list[Path]) -> list[dict]:
    media_type = ctx["mediaType"]
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    plans = []
    seen = set()
    for index, source in enumerate(sources):
        source_info = validate_video(source, minimum)
        if media_type == "movie":
            if len(sources) != 1:
                raise RuntimeError("电影任务必须明确提供单个主视频")
            relative = render_path(ctx["naming"]["movieFile"], ctx["namingFields"])
            season = episode = None
        else:
            parsed = episode_from_name(source, ctx["args"].season)
            if not parsed:
                if len(sources) == 1:
                    parsed = (ctx["args"].season, ctx["args"].episode or 1)
                else:
                    raise RuntimeError(f"无法识别集号: {source.name}；请规范为 SxxExx/EPxx")
            season, episode = parsed
            fields = {**ctx["namingFields"], "season": season, "episode": episode}
            relative = render_path(ctx["naming"]["seasonDir"], fields) / render_path(ctx["naming"]["episodeFile"], fields)
        if relative in seen:
            raise RuntimeError(f"多个来源映射到同一输出: {relative}")
        seen.add(relative)
        stat = source.stat()
        plans.append({"source": source, "sourceInfo": source_info, "sourceSize": stat.st_size, "sourceMtimeNs": stat.st_mtime_ns, "relative": relative, "season": season, "episode": episode, "index": index})
    return plans


def ffmpeg_command(ctx: dict, source: Path, target: Path) -> list[str]:
    profile = ctx["profile"]
    codec = str(profile.get("videoCodec", "libx264"))
    audio_codec = str(profile.get("audioCodec", "aac"))
    command = ["ffmpeg", "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn"]
    if codec == "copy":
        command += ["-c:v", "copy"]
    else:
        resolution = int(profile.get("resolution", 1080))
        command += ["-vf", f"scale=-2:min(ih\\,{resolution})", "-c:v", codec]
        if profile.get("preset"):
            command += ["-preset", str(profile["preset"])]
        if profile.get("crf") is not None:
            command += ["-crf", str(profile["crf"])]
        elif profile.get("videoBitrate"):
            command += ["-b:v", str(profile["videoBitrate"])]
    command += ["-c:a", audio_codec]
    if audio_codec != "copy" and profile.get("audioBitrate"):
        command += ["-b:a", str(profile["audioBitrate"])]
    command += ["-map_metadata", "-1", "-map_chapters", "-1"]
    if profile["container"] == "mp4":
        command += ["-movflags", "+faststart"]
    command.append(str(target))
    return command


def transcode(ctx: dict, plans: list[dict]) -> None:
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    for plan in plans:
        output = ctx["outputRoot"] / plan["relative"]
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(f".{output.stem}.partial{output.suffix}")
        temp.unlink(missing_ok=True)
        status_update(ctx["id"], phase="transcoding", currentFile=plan["source"].name)
        log(ctx, f"转码: {plan['source'].name} -> {plan['relative']}")
        run_child(ctx, ffmpeg_command(ctx, plan["source"], temp), "transcoding")
        source_stat = plan["source"].stat()
        if source_stat.st_size != plan["sourceSize"] or source_stat.st_mtime_ns != plan["sourceMtimeNs"]:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"转码期间来源仍在变化: {plan['source'].name}")
        output_info = validate_video(temp, minimum)
        if output_info["duration"] < plan["sourceInfo"]["duration"] * 0.95:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"转码输出疑似截断: {plan['source'].name}")
        os.replace(temp, output)
        plan["output"] = output
        for suffix in SUBTITLE_EXTS:
            sidecar = plan["source"].with_suffix(suffix)
            if sidecar.is_file() and not sidecar.is_symlink():
                shutil.copy2(sidecar, output.with_suffix(suffix))


def xml_text(parent: ET.Element, name: str, value) -> None:
    if value not in (None, "", []):
        ET.SubElement(parent, name).text = str(value)


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    ET.ElementTree(root).write(temp, encoding="utf-8", xml_declaration=True)
    os.replace(temp, path)


def write_nfo(ctx: dict, plans: list[dict]) -> None:
    metadata = ctx["metadata"]
    root_name = "tvshow" if ctx["mediaType"] == "tv" else "movie"
    root = ET.Element(root_name)
    xml_text(root, "title", metadata.get("title"))
    xml_text(root, "originaltitle", metadata.get("originalTitle"))
    xml_text(root, "sorttitle", metadata.get("title"))
    xml_text(root, "year", metadata.get("year"))
    xml_text(root, "premiered", metadata.get("premiered"))
    xml_text(root, "plot", metadata.get("plot"))
    xml_text(root, "studio", metadata.get("studio"))
    for genre in metadata.get("genres", []):
        xml_text(root, "genre", genre)
    ids = metadata.get("ids", {}) if isinstance(metadata.get("ids"), dict) else {}
    first = True
    for id_type, value in ids.items():
        if value:
            element = ET.SubElement(root, "uniqueid", {"type": str(id_type), "default": "true" if first else "false"})
            element.text = str(value)
            first = False
    write_xml(ctx["outputRoot"] / f"{root_name}.nfo", root)
    if ctx["mediaType"] == "tv":
        episode_items = metadata.get("episodes", []) if isinstance(metadata.get("episodes"), list) else []
        for plan in plans:
            details = next((item for item in episode_items if isinstance(item, dict) and int(item.get("season", -1)) == plan["season"] and int(item.get("episode", -1)) == plan["episode"]), {})
            episode_root = ET.Element("episodedetails")
            xml_text(episode_root, "title", details.get("title") or f"Episode {plan['episode']}")
            xml_text(episode_root, "season", plan["season"])
            xml_text(episode_root, "episode", plan["episode"])
            xml_text(episode_root, "aired", details.get("aired"))
            xml_text(episode_root, "plot", details.get("plot"))
            xml_text(episode_root, "runtime", details.get("runtime"))
            write_xml(plan["output"].with_suffix(".nfo"), episode_root)


def download_image(source: str, destination: Path, ctx: dict) -> None:
    validate_source(source)
    temp_source = ctx["workRoot"] / f".artwork-source{Path(urllib.parse.urlsplit(source).path).suffix or '.img'}"
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "media-downloader/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response, open(temp_source, "wb") as handle:
                remaining = 25 * 1024 * 1024
                while remaining > 0:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
                if response.read(1):
                    raise RuntimeError("图片超过 25MB")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"图片源返回 HTTP {exc.code}: {redacted_source(source)}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"图片源连接失败: {redacted_source(source)}: {exc.reason}") from exc
        source_path = temp_source
    else:
        source_path = resolve_path(source)
        if not source_path.is_file():
            raise RuntimeError(f"图片不存在: {source_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path), "-frames:v", "1", str(destination)])
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"图片转换失败: {source_path.name}")
    temp_source.unlink(missing_ok=True)


def write_artwork(ctx: dict, plans: list[dict]) -> None:
    metadata = ctx["metadata"]
    roots = {ctx["sourceRoot"], *(plan["source"].parent for plan in plans)}
    source_images = sorted({path for root in roots for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTS and not path.is_symlink()})
    # ponytail: infer only conventional names; ambiguous libraries must provide metadata paths.
    poster = metadata.get("posterPath") or metadata.get("posterUrl") or next((str(path) for path in source_images if path.stem.casefold() in {"poster", "folder", "cover"}), "")
    fanart = metadata.get("fanartPath") or metadata.get("fanartUrl") or next((str(path) for path in source_images if path.stem.casefold() in {"fanart", "backdrop", "background"}), "")
    required = bool(ctx["config"].get("metadata", {}).get("requireArtwork", False))
    for kind, source in (("poster", poster), ("fanart", fanart)):
        if not source:
            if required and kind == "poster":
                raise RuntimeError("未找到必需的海报")
            continue
        try:
            download_image(str(source), ctx["outputRoot"] / f"{kind}.jpg", ctx)
        except Exception as exc:
            if required:
                raise
            log(ctx, f"警告: {kind} 获取失败: {exc}")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def existing_matches(source: Path, target: Path, minimum_duration: float) -> bool:
    if target.is_symlink() or not target.is_file() or source.stat().st_size != target.stat().st_size:
        return False
    if source.suffix.lower() in VIDEO_EXTS:
        validate_video(target, minimum_duration)
    return file_digest(source) == file_digest(target)


def atomic_copy(source: Path, target: Path, minimum_duration: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.partial")
    temp.unlink(missing_ok=True)
    try:
        with open(source, "rb") as src, open(temp, "wb") as dst:
            while True:
                if STOP_REQUESTED:
                    raise InterruptedError("任务已停止")
                chunk = src.read(8 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, temp)
        if source.stat().st_size != temp.stat().st_size:
            raise RuntimeError(f"复制大小不一致: {source.name}")
        if source.suffix.lower() in VIDEO_EXTS:
            validate_video(temp, minimum_duration)
        if file_digest(source) != file_digest(temp):
            raise RuntimeError(f"复制哈希不一致: {source.name}")
        try:
            os.link(temp, target)
        except FileExistsError:
            if not existing_matches(temp, target, minimum_duration):
                raise RuntimeError(f"目标已被其他任务写入且内容不同，拒绝覆盖: {target}")
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                raise
            # ponytail: some NAS filesystems lack hard links; exclusive creation keeps no-clobber safety.
            created_identity = None
            try:
                with open(temp, "rb") as src, open(target, "xb") as dst:
                    created = os.fstat(dst.fileno())
                    created_identity = (created.st_dev, created.st_ino)
                    while chunk := src.read(8 * 1024 * 1024):
                        if STOP_REQUESTED:
                            raise InterruptedError("任务已停止")
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if temp.stat().st_size != target.stat().st_size or file_digest(temp) != file_digest(target):
                    raise RuntimeError(f"复制哈希不一致: {source.name}")
                shutil.copystat(temp, target)
            except FileExistsError:
                if not existing_matches(temp, target, minimum_duration):
                    raise RuntimeError(f"目标已被其他任务写入且内容不同，拒绝覆盖: {target}")
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    current = target.stat(follow_symlinks=False)
                    if created_identity == (current.st_dev, current.st_ino):
                        target.unlink()
                raise
    finally:
        temp.unlink(missing_ok=True)


def archive(ctx: dict) -> list[str]:
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    output_root = ctx["outputRoot"].resolve()
    files = sorted(path for path in output_root.rglob("*") if is_safe_file(output_root, path))
    if not files:
        raise RuntimeError("没有可归档文件")
    archived = []
    for source in files:
        if STOP_REQUESTED:
            raise InterruptedError("任务已停止")
        relative = source.relative_to(output_root)
        target = ctx["targetShow"] / relative
        if not target.resolve(strict=False).is_relative_to(ctx["targetRoot"]):
            raise RuntimeError("归档目标逃逸")
        if target.exists():
            if not existing_matches(source, target, minimum):
                raise RuntimeError(f"目标已存在且内容不同，拒绝覆盖: {target}")
        else:
            log(ctx, f"归档: {relative}")
            atomic_copy(source, target, minimum)
            if not target.is_file() or source.stat().st_size != target.stat().st_size:
                raise RuntimeError(f"归档校验失败: {target}")
        archived.append(str(target))
        status_update(ctx["id"], phase="archiving", currentFile=str(relative), archivedFiles=archived)
    return archived


def cleanup_work(ctx: dict) -> None:
    remove_owned_work(ctx)


def pipeline(args) -> int:
    config = load_config()
    ctx = build_context(config, args)
    source, requested_downloader = resolve_source(args)
    plan = {
        "taskId": ctx["id"], "title": ctx["canonical"], "mediaType": ctx["mediaType"],
        "requestedTitle": ctx["title"],
        "profile": ctx["profileName"], "target": ctx["targetName"],
        "naming": ctx["namingName"],
        "targetPath": str(ctx["targetShow"]), "source": redacted_source(source),
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    ensure_work(ctx, source)
    with task_lock(ctx):
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        status_update(
            ctx["id"], **plan, phase="starting", pid=os.getpid(), childPid=None,
            currentOperation="starting", currentFile="", startedAt=now(), logPath=str(ctx["logPath"]),
        )
        try:
            sources = acquire(ctx, source, requested_downloader)
            plans = planned_outputs(ctx, sources)
            transcode(ctx, plans)
            status_update(ctx["id"], phase="metadata", currentOperation="metadata", currentFile="")
            write_nfo(ctx, plans)
            write_artwork(ctx, plans)
            status_update(ctx["id"], phase="archiving", currentOperation="archiving")
            archived = archive(ctx)
            if not args.keep_work:
                cleanup_work(ctx)
            status_update(ctx["id"], phase="done", currentOperation="done", currentFile="", pid=None, childPid=None, archivedFiles=archived, finishedAt=now())
            log(ctx, f"完成: {ctx['canonical']}")
            return 0
        except InterruptedError as exc:
            status_update(ctx["id"], phase="stopped", currentOperation="stopped", lastError=str(exc), pid=None, childPid=None)
            log(ctx, str(exc))
            return 130
        except Exception as exc:
            status_update(ctx["id"], phase="failed", currentOperation="failed", lastError=str(exc), pid=None, childPid=None)
            log(ctx, f"失败: {exc}")
            raise


def command_profiles(_args) -> int:
    config = load_config()
    print(json.dumps({"defaultProfiles": config.get("defaultProfiles", {}), "defaultNaming": config.get("defaultNaming", "plex"), "profiles": config.get("profiles", {}), "targets": config.get("targets", {}), "namingPresets": config.get("namingPresets", {})}, ensure_ascii=False, indent=2))
    return 0


def command_check(args) -> int:
    states = status_read()
    if args.title:
        title = args.title.casefold()
        states = {key: value for key, value in states.items() if title in str(value.get("title", "")).casefold()}
    print(json.dumps(states, ensure_ascii=False, indent=2))
    return 0 if states else 1


def process_matches(pid: int, title: str) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.returncode == 0 and "media-downloader.py" in result.stdout and title in result.stdout


def command_stop(args) -> int:
    matches = []
    for identifier, state in status_read().items():
        if args.title.casefold() == str(state.get("title", "")).casefold() and state.get("phase") not in {"done", "failed", "stopped"}:
            matches.append((identifier, state))
    if not matches:
        print("未找到运行中的任务")
        return 1
    for identifier, state in matches:
        pid = state.get("pid")
        process_title = str(state.get("requestedTitle") or state.get("title", ""))
        if isinstance(pid, int) and process_matches(pid, process_title):
            os.kill(pid, signal.SIGTERM)
            print(f"已发送停止信号: {state.get('title')} pid={pid}")
        else:
            status_update(identifier, phase="stopped", lastError="stale process")
    return 0


def command_doctor(_args) -> int:
    config = load_config()
    checks = []
    for tool, required in (("ffmpeg", True), ("ffprobe", True), ("aria2c", False), ("yt-dlp", False)):
        location = shutil.which(tool)
        checks.append({"name": tool, "status": "ok" if location else ("error" if required else "optional-missing"), "path": location or ""})
    for name, raw in (config.get("targets", {}) or {}).items():
        value = raw.get("path") if isinstance(raw, dict) else raw
        path = resolve_path(value)
        try:
            require_target_root(path)
            status = "ok"
        except RuntimeError:
            status = "unavailable"
        checks.append({"name": f"target:{name}", "status": status, "path": str(path)})
    for name, source in (config.get("searchSources", {}) or {}).items():
        if isinstance(source, dict) and source.get("type") == "jackett" and source.get("enabled", True):
            env_name = str(source.get("apiKeyEnv", "JACKETT_API_KEY"))
            checks.append({"name": f"search:{name}", "status": "ok" if os.environ.get(env_name) else "error", "detail": env_name})
    metadata = config.get("metadata", {})
    if metadata.get("provider") == "tmdb":
        env_name = str(metadata.get("apiKeyEnv", "TMDB_API_KEY"))
        checks.append({"name": "metadata:tmdb", "status": "ok" if os.environ.get(env_name) else "optional-missing", "detail": env_name})
    print(json.dumps({"config": str(config_file()), "checks": checks}, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] == "error" for item in checks) else 0


def add_pipeline_arguments(parser: argparse.ArgumentParser, source_required: bool = True) -> None:
    parser.add_argument("title")
    parser.add_argument("source", nargs=None if source_required else "?")
    parser.add_argument("--candidate")
    parser.add_argument("--source-file", help="权限为 0600、只含一个来源的文件；用于带凭据 URL")
    parser.add_argument("--type", dest="media_type", choices=("tv", "movie"), default="tv")
    parser.add_argument("--year", type=int)
    parser.add_argument("--profile")
    parser.add_argument("--target")
    parser.add_argument("--naming")
    parser.add_argument("--metadata", help="Agent 提供的 metadata JSON")
    parser.add_argument("--downloader", choices=("auto", "aria2", "yt-dlp", "local"), default="auto")
    parser.add_argument("--season", type=int, default=1)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--playlist", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--reset-work", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.set_defaults(handler=pipeline)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Agent media ingest pipeline")
    commands = root.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="搜索配置的结构化来源并缓存候选")
    search.add_argument("query")
    search.add_argument("--source", action="append")
    search.add_argument("--type", dest="media_type", choices=("tv", "movie"), default="tv")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(handler=command_search)
    for name in ("ingest", "resume", "download"):
        add_pipeline_arguments(commands.add_parser(name, help="获取、转码、整理并归档"), source_required=False)
    for name in ("adopt", "process", "organize"):
        add_pipeline_arguments(commands.add_parser(name, help="处理本地媒体并归档"), source_required=True)
    profiles = commands.add_parser("profiles", aliases=["profile"])
    profiles.set_defaults(handler=command_profiles)
    check = commands.add_parser("check", aliases=["status"])
    check.add_argument("title", nargs="?")
    check.set_defaults(handler=command_check)
    stop = commands.add_parser("stop", aliases=["cancel"])
    stop.add_argument("title")
    stop.set_defaults(handler=command_stop)
    doctor = commands.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.handler(args) or 0)
    except (ValueError, RuntimeError, TimeoutError, OSError, urllib.error.URLError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
