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
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SKILL_DIR / ".runtime"
DEFAULT_CONFIG_FILE = SKILL_DIR / "config.json"
VERSION = "0.4.0"
CONFIG_SCHEMA_VERSION = 1
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".rm", ".rmvb", ".3gp"}
SUBTITLE_EXTS = {".srt", ".smi", ".ass", ".ssa", ".vtt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tbn"}
ARCHIVE_EXTS = VIDEO_EXTS | SUBTITLE_EXTS | IMAGE_EXTS | {".nfo"}
REPAIR_SIDECAR_EXTS = SUBTITLE_EXTS | IMAGE_EXTS | {".nfo"}
TV_SHARED_MERGE_FILES = {"tvshow.nfo", "poster.jpg", "fanart.jpg", "banner.jpg", "clearlogo.png"}
YTDLP_BROWSERS = {"brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale"}
MAX_COMPONENT_BYTES = 200
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
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        if private:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(descriptor)
        raise RuntimeError(f"日志文件不安全: {path}")
    os.fchmod(descriptor, 0o600)
    os.set_blocking(descriptor, True)
    os.close(descriptor)


def write_private_text(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def open_private_input(path: Path, label: str, max_size: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > max_size
    ):
        os.close(descriptor)
        raise RuntimeError(f"{label}：必须是当前用户拥有、单硬链接、无组/其他权限且不超过 {max_size // 1024}KB 的普通文件")
    return descriptor


def require_private_input(path: Path, label: str, max_size: int) -> Path:
    os.close(open_private_input(path, label, max_size))
    return path


def read_private_text(path: Path) -> str:
    descriptor = open_private_input(path, "--source-file", 64 * 1024)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        return handle.read().strip()


def config_file() -> Path:
    return Path(os.environ.get("MEDIA_DOWNLOADER_CONFIG", DEFAULT_CONFIG_FILE)).expanduser().resolve()


def validate_config(data: dict) -> None:
    profiles = data.get("profiles")
    targets = data.get("targets", {})
    naming = data.get("namingPresets")
    if not isinstance(profiles, dict) or not profiles:
        raise RuntimeError("配置缺少 profiles")
    if not isinstance(targets, dict):
        raise RuntimeError("targets 必须是对象")
    if not isinstance(naming, dict) or not naming:
        raise RuntimeError("配置缺少 namingPresets")
    try:
        timeout_hours = float(data.get("timeoutHours", 24))
        minimum_duration = float(data.get("minMediaDurationSeconds", 120))
        bt_stop_timeout = int(data.get("btStopTimeoutSeconds", 600))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("timeoutHours/minMediaDurationSeconds/btStopTimeoutSeconds 必须是数字") from exc
    if timeout_hours <= 0 or minimum_duration < 0 or not 0 <= bt_stop_timeout <= 86400:
        raise RuntimeError("timeoutHours 必须大于 0，minMediaDurationSeconds 不得小于 0，btStopTimeoutSeconds 必须在 0-86400 秒")
    download_dir = data.get("downloadDir")
    if download_dir not in (None, "") and (not isinstance(download_dir, str) or not download_dir.strip()):
        raise RuntimeError("downloadDir 必须是非空路径字符串")
    defaults = data.get("defaultProfiles", {})
    if not isinstance(defaults, dict):
        raise RuntimeError("defaultProfiles 必须是对象")
    for media_type in ("tv", "movie"):
        profile_name = defaults.get(media_type)
        profile = profiles.get(profile_name)
        if not isinstance(profile, dict) or profile.get("type") != media_type:
            raise RuntimeError(f"defaultProfiles.{media_type} 未指向有效的 {media_type} 预设")
    default_modes = data.get("defaultModes", {})
    if not isinstance(default_modes, dict):
        raise RuntimeError("defaultModes 必须是对象")
    for media_type in ("tv", "movie"):
        if default_modes.get(media_type, "transcode") not in {"transcode", "organize"}:
            raise RuntimeError(f"defaultModes.{media_type} 必须是 transcode 或 organize")
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or profile.get("type") not in {"tv", "movie"}:
            raise RuntimeError(f"profile 无效: {name}")
        naming_name = profile.get("naming") or data.get("defaultNaming") or "plex"
        if naming_name not in naming:
            raise RuntimeError(f"profile {name} 引用了未知 naming: {naming_name}")
        container = str(profile.get("container", "mp4")).lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{2,8}", container):
            raise RuntimeError(f"profile {name} 容器无效: {container}")
        codec = profile.get("videoCodec", "libx264")
        audio_codec = profile.get("audioCodec", "aac")
        if not isinstance(codec, str) or not codec or not isinstance(audio_codec, str) or not audio_codec:
            raise RuntimeError(f"profile {name} codec 无效")
        if codec != "copy":
            try:
                resolution = int(profile.get("resolution", 1080))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"profile {name} resolution 无效") from exc
            if not 64 <= resolution <= 4320:
                raise RuntimeError(f"profile {name} resolution 超出范围: {resolution}")
        if profile.get("crf") is not None:
            try:
                crf = float(profile["crf"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"profile {name} CRF 无效") from exc
            if not 0 <= crf <= 63:
                raise RuntimeError(f"profile {name} CRF 超出范围: {crf}")
    for name, value in targets.items():
        raw = value.get("path") if isinstance(value, dict) else value
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(f"target 路径无效: {name}")
    sources = data.get("searchSources", {})
    if not isinstance(sources, dict):
        raise RuntimeError("searchSources 必须是对象")
    for name, source in sources.items():
        if not isinstance(source, dict) or source.get("type") not in {"jackett", "torznab", "web"}:
            raise RuntimeError(f"搜索源无效: {name}")
        try:
            search_timeout = int(source.get("timeoutSeconds", 90))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"搜索源 {name} timeoutSeconds 必须是整数") from exc
        if not 1 <= search_timeout <= 300:
            raise RuntimeError(f"搜索源 {name} timeoutSeconds 必须在 1-300 秒")
        if source["type"] == "jackett":
            value = str(source.get("url", ""))
            parsed = urllib.parse.urlsplit(validate_public_source_url(value))
        elif source["type"] == "torznab":
            value = str(source.get("url", ""))
            parsed = urllib.parse.urlsplit(validate_public_source_url(value))
        else:
            template = str(source.get("urlTemplate", ""))
            parsed = urllib.parse.urlsplit(validate_public_source_url(template, template=True).replace("{query}", "test"))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RuntimeError(f"搜索源 URL 无效: {name}")
    words = data.get("customWords", {})
    if not isinstance(words, dict):
        raise RuntimeError("customWords 必须是对象")
    for field in ("ignore", "replace", "episodeOffset"):
        if field in words and not isinstance(words[field], list):
            raise RuntimeError(f"customWords.{field} 必须是数组")
    for item in words.get("ignore", []):
        if not isinstance(item, str) or not item:
            raise RuntimeError("customWords.ignore 每项必须是非空字符串")
    for item in words.get("replace", []):
        if not isinstance(item, dict) or not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str) or not item["from"]:
            raise RuntimeError('customWords.replace 每项必须是 {"from": ..., "to": ...}')
    for item in words.get("episodeOffset", []):
        if not isinstance(item, dict) or not isinstance(item.get("pattern"), str) or not item["pattern"]:
            raise RuntimeError('customWords.episodeOffset 每项必须是 {"pattern": ..., "offset": ±整数}')
        try:
            re.compile(item["pattern"])
            offset = int(item.get("offset", 0))
        except (TypeError, ValueError, re.error) as exc:
            raise RuntimeError(f"customWords.episodeOffset 项无效: {item}") from exc
        if not -10000 <= offset <= 10000:
            raise RuntimeError(f"customWords.episodeOffset 偏移超出范围: {offset}")


def load_config() -> dict:
    if not config_file().is_file():
        raise RuntimeError(f"配置不存在: {config_file()}；请复制 config.example.json 为 config.json")
    data = read_json(config_file())
    if not isinstance(data, dict):
        raise RuntimeError("配置根节点必须是对象")
    validate_config(data)
    return data


def validate_public_source_url(value: str, *, template: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value.replace("{query}", "test") if template else value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("来源 URL 必须是无内嵌凭据的 HTTP(S) URL")
    if any(is_sensitive_name(key) for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("来源 URL 不得内嵌密钥或签名；请使用 apiKeyEnv")
    if template and "{query}" not in value:
        raise ValueError("网页来源 URL 模板必须包含 {query}")
    return value


def is_sensitive_name(value: str) -> bool:
    normalized = re.sub(r"[-_]", "", value).casefold()
    return normalized in {"key", "auth", "authorization", "sig"} or normalized.endswith(
        ("apikey", "token", "password", "passwd", "secret", "signature")
    )


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
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
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
    if len(value.encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise ValueError(f"名称超过 {MAX_COMPONENT_BYTES} 字节: {value[:40]}…")
    return value


def validate_title(title: str) -> str:
    if not title or title.strip() in {".", ".."}:
        raise ValueError("请提供有效标题")
    if len(title) > 200 or any(ord(char) < 32 for char in title) or "/" in title or "\\" in title:
        raise ValueError("标题包含不安全字符")
    return sanitize_component(title)


def resolve_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve(strict=False)


def configured_download_root(config: dict) -> Path | None:
    value = os.environ.get("MEDIA_DOWNLOADER_DOWNLOAD_DIR") or config.get("downloadDir")
    return resolve_path(value) if value else None


def contains_path(parent: Path, child: Path) -> bool:
    return child == parent or child.is_relative_to(parent)


def paths_overlap(left: Path, right: Path) -> bool:
    return contains_path(left, right) or contains_path(right, left)


def require_mounted_volume(path: Path, label: str) -> None:
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        volume_root = Path("/Volumes") / parts[2]
        if not os.path.ismount(volume_root):
            raise RuntimeError(f"{label}所在磁盘未挂载: {volume_root}")


def require_target_root(path: Path) -> None:
    forbidden = {Path("/"), Path.home().resolve(), Path("/Volumes")}
    if path in forbidden:
        raise RuntimeError(f"目标目录范围过大，拒绝使用: {path}")
    require_mounted_volume(path, "目标目录")
    if not path.is_dir():
        raise RuntimeError(f"目标预设目录不可用: {path}")
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"目标预设目录不可写: {path}")


def require_work_root(path: Path, label: str) -> None:
    if path in {Path("/"), Path.home().resolve(), Path("/Volumes")}:
        raise RuntimeError(f"{label}范围过大，拒绝使用: {path}")
    require_mounted_volume(path, label)
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir() or not os.access(existing, os.W_OK):
        raise RuntimeError(f"{label}不可写: {path}")


def validate_download_root(base_root: Path, download_root: Path) -> None:
    require_work_root(download_root, "下载成品目录")
    work_parent = (base_root / ".media-downloader-work").resolve(strict=False)
    if contains_path(work_parent, download_root.resolve(strict=False)):
        raise RuntimeError(f"下载成品目录不得位于任务工作区内: {download_root}")


def directory_identity(path: Path) -> tuple[int, int]:
    stat = path.lstat()
    return stat.st_dev, stat.st_ino


def ensure_target_parent(ctx: dict, parent: Path) -> None:
    root = ctx["targetRoot"]
    require_target_root(root)
    if directory_identity(root) != ctx["targetIdentity"]:
        raise RuntimeError(f"目标目录在任务期间发生变化: {root}")
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"归档目标逃逸: {parent}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir() or not current.resolve().is_relative_to(root):
            raise RuntimeError(f"归档目录不安全: {current}")
    if directory_identity(root) != ctx["targetIdentity"]:
        raise RuntimeError(f"目标目录在任务期间发生变化: {root}")


def normalize_year(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"年份无效: {value}") from exc
    if not 1000 <= year <= 2999:
        raise ValueError(f"年份超出范围: {year}")
    return year


def canonical_name(title: str, year: int | None) -> str:
    title = sanitize_component(title)
    if year and not re.search(rf"\({year}\)$", title):
        return f"{title} ({year})"
    return title


def task_id(media_type: str, canonical: str, target_root: Path) -> str:
    digest = hashlib.sha256(f"{media_type}\0{canonical}\0{target_root}".encode()).hexdigest()[:12]
    return f"{media_type}-{digest}"


def pipeline_task_id(ctx: dict, source: str) -> str:
    # 下载/转换/整理任务：身份含来源，同标题换源重下互不覆盖
    digest = hashlib.sha256(f"{ctx['mediaType']}\0{ctx['canonical']}\0{ctx['targetRoot']}\0{source}".encode()).hexdigest()[:12]
    return f"{ctx['mediaType']}-{digest}"


def default_profile_name(config: dict, media_type: str) -> str:
    return str(config["defaultProfiles"][media_type])


def select_profile(config: dict, media_type: str, name: str | None) -> tuple[str, dict]:
    profiles = config.get("profiles", {})
    name = name or default_profile_name(config, media_type)
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise RuntimeError(f"转码预设不存在: {name}")
    if profile.get("type", media_type) != media_type:
        raise RuntimeError(f"预设 {name} 不适用于 {media_type}")
    container = str(profile.get("container", "mp4")).lower().lstrip(".")
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
    raise RuntimeError("未配置目标预设")


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


def read_response(response, limit: int, label: str) -> bytes:
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise RuntimeError(f"{label} 响应超过 {limit // (1024 * 1024)}MB")
    return payload


def http_json(url: str, params: dict, headers: dict | None = None, timeout: int = 20):
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "media-downloader/2.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(read_response(response, 5 * 1024 * 1024, urllib.parse.urlsplit(url).netloc).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc} 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc} 连接失败: {exc.reason}") from exc


def fetch_tmdb(config: dict, media_type: str, title: str, year: int | None, season: int | None = None) -> dict:
    metadata_config = config.get("metadata", {})
    key_env = metadata_config.get("apiKeyEnv", "TMDB_API_KEY")
    api_key = os.environ.get(str(key_env), "")
    if not api_key:
        return {}
    language = metadata_config.get("language", "zh-CN")
    kind = "tv" if media_type == "tv" else "movie"
    # TMDB v4 只读 token 是 JWT（eyJ 开头），必须走 Bearer header；v3 key 走 api_key 参数
    if api_key.startswith("eyJ"):
        auth = {"Authorization": f"Bearer {api_key}"}
        auth_params = {}
    else:
        auth = None
        auth_params = {"api_key": api_key}
    # 用户常输入带年份的标题（"The Odyssey 2026"），TMDB 的 query 是全文匹配会 0 命中；
    # 剥离末尾年份单独走 year 过滤参数，标题回到干净检索词。
    query = title
    if year is None:
        match = re.match(r"^(?P<name>.+?)\s*[\(\[]?(?P<year>(?:19|20)\d{2})[\)\]]?$", title.strip())
        if match:
            query = match.group("name").strip()
            year = int(match.group("year"))
    params = {**auth_params, "language": language, "query": query}
    params["first_air_date_year" if kind == "tv" else "year"] = year
    search = http_json(f"https://api.themoviedb.org/3/search/{kind}", params, auth)
    results = search.get("results", []) if isinstance(search, dict) else []
    if not results:
        return {}
    item = results[0]
    tmdb_id = item.get("id")
    detail = http_json(
        f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
        {**auth_params, "language": language, "append_to_response": "external_ids,credits"},
        auth,
    )
    date_key = "first_air_date" if kind == "tv" else "release_date"
    name_key = "name" if kind == "tv" else "title"
    original_key = "original_name" if kind == "tv" else "original_title"
    date = detail.get(date_key) or item.get(date_key) or ""
    externals = detail.get("external_ids", {}) if isinstance(detail.get("external_ids"), dict) else {}
    credits = detail.get("credits", {}) if isinstance(detail.get("credits"), dict) else {}
    crew = credits.get("crew", []) if isinstance(credits.get("crew"), list) else []
    countries = detail.get("production_countries", []) if isinstance(detail.get("production_countries"), list) else []
    episodes = []
    if kind == "tv" and season is not None:
        try:
            season_detail = http_json(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}",
                {**auth_params, "language": language}, auth,
            )
            for episode in season_detail.get("episodes", []) if isinstance(season_detail, dict) else []:
                episode_crew = episode.get("crew", []) if isinstance(episode.get("crew"), list) else []
                episodes.append({
                    "season": int(episode.get("season_number", season)),
                    "episode": int(episode["episode_number"]),
                    "title": episode.get("name") or "",
                    "plot": episode.get("overview") or "",
                    "aired": episode.get("air_date") or "",
                    "runtime": episode.get("runtime"),
                    "rating": episode.get("vote_average"),
                    "directors": [person.get("name") for person in episode_crew if person.get("job") == "Director" and person.get("name")],
                    "writers": [person.get("name") for person in episode_crew if person.get("department") == "Writing" and person.get("name")],
                    "ids": {"tmdb": episode.get("id")},
                    "thumbUrl": f"https://image.tmdb.org/t/p/original{episode.get('still_path')}" if episode.get("still_path") else "",
                })
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            print(f"警告: TMDB 第 {season} 季分集详情查询失败: {exc}", file=sys.stderr)
    return {
        "title": detail.get(name_key) or item.get(name_key),
        "originalTitle": detail.get(original_key) or item.get(original_key),
        "year": int(date[:4]) if str(date)[:4].isdigit() else None,
        "premiered": date,
        "plot": detail.get("overview") or item.get("overview") or "",
        "tagline": detail.get("tagline") or "",
        "rating": detail.get("vote_average"),
        "ratingVotes": detail.get("vote_count"),
        "ratingSource": "themoviedb",
        "runtime": detail.get("runtime") or next(iter(detail.get("episode_run_time") or []), None),
        "status": detail.get("status") or "",
        "genres": [genre.get("name") for genre in detail.get("genres", []) if genre.get("name")],
        "countries": [country.get("name") for country in countries if country.get("name")],
        "directors": [person.get("name") for person in crew if person.get("job") == "Director" and person.get("name")],
        "writers": [person.get("name") for person in crew if person.get("department") == "Writing" and person.get("name")],
        "actors": [{"name": person.get("name"), "role": person.get("character")} for person in (credits.get("cast") or [])[:20] if person.get("name")],
        "studio": next((company.get("name") for company in detail.get("production_companies", []) if company.get("name")), ""),
        "ids": {"tmdb": tmdb_id, "imdb": externals.get("imdb_id"), "tvdb": externals.get("tvdb_id")},
        "posterUrl": f"https://image.tmdb.org/t/p/original{detail.get('poster_path')}" if detail.get("poster_path") else "",
        "fanartUrl": f"https://image.tmdb.org/t/p/original{detail.get('backdrop_path')}" if detail.get("backdrop_path") else "",
        "episodes": episodes,
    }


def fetch_tvmaze(title: str, season: int | None = None) -> dict:
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
    episodes = []
    if season is not None and show.get("id"):
        try:
            items = http_json(f"https://api.tvmaze.com/shows/{show['id']}/episodes", {})
            episodes = [{
                "season": int(item["season"]), "episode": int(item["number"]),
                "title": item.get("name") or "", "plot": re.sub(r"<[^>]+>", "", item.get("summary") or ""),
                "aired": item.get("airdate") or "", "runtime": item.get("runtime"),
                "ids": {"tvmaze": item.get("id")},
            } for item in items if isinstance(item, dict) and item.get("season") == season and item.get("number")]
        except (TypeError, ValueError, RuntimeError) as exc:
            print(f"警告: TVMaze 第 {season} 季分集详情查询失败: {exc}", file=sys.stderr)
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
        "episodes": episodes,
    }


def resolve_metadata(config: dict, args) -> dict:
    supplied = metadata_from_file(args.metadata)
    # 识别词作用于检索词，让 "狂飙.全39集" 这类标题能命中 TMDB；不改写 metadata.title 存储
    query_title, episode_offset = apply_custom_words(config, args.media_type, args.title)
    args.episode_offset = episode_offset
    query_title = query_title or args.title
    fetched = {}
    offline = bool(args.offline or os.environ.get("MEDIA_DOWNLOADER_OFFLINE") == "1")
    metadata_config = config.get("metadata", {})
    if not offline and metadata_config.get("provider") == "tmdb":
        try:
            fetched = fetch_tmdb(config, args.media_type, query_title, args.year, args.season if args.media_type == "tv" else None)
        except Exception as exc:
            print(f"警告: TMDB 元数据查询失败: {exc}", file=sys.stderr)
    if not fetched and not offline and args.media_type == "tv" and metadata_config.get("tvFallback", "tvmaze") == "tvmaze":
        try:
            fetched = fetch_tvmaze(query_title, args.season)
        except Exception as exc:
            print(f"警告: TVMaze 元数据查询失败: {exc}", file=sys.stderr)
    metadata = {**fetched, **supplied}
    metadata["title"] = metadata.get("title") or args.title
    if not isinstance(metadata["title"], str) or not metadata["title"].strip():
        raise ValueError("metadata.title 必须是非空字符串")
    metadata["year"] = normalize_year(args.year if args.year is not None else metadata.get("year"))
    metadata["originalTitle"] = metadata.get("originalTitle") or metadata["title"]
    metadata.setdefault("plot", "")
    metadata["premiered"] = metadata.get("premiered") or (f"{metadata['year']}-01-01" if metadata.get("year") else "")
    genres = metadata.get("genres") or []
    if not isinstance(genres, list):
        raise ValueError("metadata.genres 必须是数组")
    metadata["genres"] = genres
    metadata["studio"] = metadata.get("studio") or ""
    for field in ("countries", "tags", "directors", "writers"):
        values = metadata.get(field) or []
        if not isinstance(values, list):
            raise ValueError(f"metadata.{field} 必须是数组")
        metadata[field] = values
    actors = metadata.get("actors") or []
    if not isinstance(actors, list) or any(not isinstance(item, (str, dict)) for item in actors):
        raise ValueError("metadata.actors 必须是字符串或对象数组")
    metadata["actors"] = actors
    ids = metadata.get("ids") or {}
    if not isinstance(ids, dict):
        raise ValueError("metadata.ids 必须是对象")
    metadata["ids"] = ids
    if args.media_type == "tv":
        episodes = metadata.get("episodes") or []
        if not isinstance(episodes, list):
            raise ValueError("metadata.episodes 必须是数组")
        for item in episodes:
            if not isinstance(item, dict):
                raise ValueError("metadata.episodes 每项必须是对象")
            try:
                season = int(item["season"])
                episode = int(item["episode"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("metadata.episodes 每项必须包含整数 season/episode") from exc
            if season < 0 or episode < 1:
                raise ValueError(f"metadata.episodes 季集号无效: S{season:02d}E{episode:02d}")
            for field in ("directors", "writers"):
                if field in item and not isinstance(item[field], list):
                    raise ValueError(f"metadata.episodes.{field} 必须是数组")
            if "ids" in item and not isinstance(item["ids"], dict):
                raise ValueError("metadata.episodes.ids 必须是对象")
            item["season"], item["episode"] = season, episode
        metadata["episodes"] = episodes
    return metadata


def build_context(config: dict, args) -> dict:
    title = validate_title(args.title)
    profile_name, profile = select_profile(config, args.media_type, args.profile)
    metadata = resolve_metadata(config, args)
    metadata_config = config.setdefault("metadata", {})
    offline = bool(args.offline or os.environ.get("MEDIA_DOWNLOADER_OFFLINE") == "1")
    if metadata_config.get("requireArtwork") and not metadata.get("posterPath") and not metadata.get("posterUrl"):
        key_env = str(metadata_config.get("apiKeyEnv", "TMDB_API_KEY"))
        if offline or not os.environ.get(key_env):
            metadata_config["requireArtwork"] = False
            reason = "离线模式" if offline else f"未配置 {key_env}"
            print(f"警告: {reason}，requireArtwork 降级为海报可选", file=sys.stderr)
    canonical = canonical_name(metadata.get("title") or title, metadata.get("year"))
    naming_name, naming = select_naming(config, profile, args.media_type, args.naming)
    base_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_BASE_DIR") or config.get("baseDir") or (SKILL_DIR / "work"))
    state_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_STATE_DIR") or config.get("stateDir") or (base_root / ".state"))
    download_root = None if args.no_deliver else configured_download_root(config)
    if args.no_archive:
        target_name, target_root = ("download", download_root) if download_root is not None else ("work", base_root)
    else:
        target_name, target_root = select_target(config, profile, args.target)
    require_work_root(base_root, "工作目录")
    require_work_root(state_root, "状态目录")
    if args.no_archive and download_root is not None:
        validate_download_root(base_root, download_root)
    elif not args.no_archive:
        require_target_root(target_root)
        if paths_overlap(base_root, target_root):
            raise RuntimeError(f"工作区与目标目录不得重叠: {base_root} / {target_root}")
        if paths_overlap(state_root, target_root):
            raise RuntimeError(f"状态目录与目标目录不得重叠: {state_root} / {target_root}")
    identifier = pipeline_task_id({"mediaType": args.media_type, "canonical": canonical, "targetRoot": target_root}, resolve_source(args)[0])
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
    show_subdir = render_path(naming["showDir"], naming_fields)
    # no-archive 也让 output 直接是 Plex 就绪结构（套片名目录），拖进库即用，无需手动建目录
    output_root = work_root / "output" / show_subdir if args.no_archive else work_root / "output"
    if args.no_archive:
        target_show = download_root / show_subdir if download_root is not None else output_root
    else:
        target_show = target_root / show_subdir
    if not args.no_archive and not contains_path(target_root, target_show):
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
        "targetIdentity": None if args.no_archive else directory_identity(target_root),
        "targetShow": target_show,
        "downloadRoot": download_root,
        "baseRoot": base_root,
        "stateRoot": state_root,
        "workRoot": work_root,
        "sourceRoot": work_root / "source",
        "outputRoot": output_root,
        "marker": work_root / ".media-downloader-owned.json",
        "logPath": state_root / "logs" / f"{identifier}.log",
        "metadata": metadata,
        "args": args,
        "config": config,
    }


def source_fingerprint(ctx: dict, source: str) -> str:
    payload = {
        "source": source,
        "canonical": ctx["canonical"],
        "mediaType": ctx["mediaType"],
        "profile": ctx["profile"],
        "naming": ctx["naming"],
        "metadata": ctx["metadata"],
        "metadataPolicy": ctx["config"].get("metadata", {}),
        "downloader": ctx["args"].downloader,
        "copyOriginal": ctx["args"].copy_original,
        "season": ctx["args"].season,
        "episode": ctx["args"].episode,
        "playlist": ctx["args"].playlist,
    }
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def remove_owned_work(ctx: dict) -> None:
    require_mounted_volume(ctx["baseRoot"], "工作目录")
    work = ctx["workRoot"].resolve()
    parent = (ctx["baseRoot"] / ".media-downloader-work").resolve()
    marker = ctx["marker"]
    ownership = read_json(marker, {}) if marker.exists() else {}
    if not work.is_relative_to(parent) or work == parent or ownership.get("taskId") != ctx["id"]:
        raise RuntimeError(f"拒绝清理未验证目录: {work}")
    shutil.rmtree(work)


def prepare_download_root(ctx: dict) -> None:
    root = ctx["downloadRoot"]
    validate_download_root(ctx["baseRoot"], root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require_target_root(root)
    ctx["targetIdentity"] = directory_identity(root)


def ensure_work(ctx: dict, source: str) -> None:
    if ctx["args"].no_archive and ctx["downloadRoot"] is not None:
        prepare_download_root(ctx)
    work = ctx["workRoot"]
    marker = ctx["marker"]
    fingerprint = source_fingerprint(ctx, source)
    work_parent = ctx["baseRoot"] / ".media-downloader-work"
    require_mounted_volume(work_parent, "工作目录")
    work_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if work_parent.is_symlink() or not work_parent.is_dir():
        raise RuntimeError(f"工作目录不安全: {work_parent}")
    os.chmod(work_parent, 0o700)
    if work.exists() and ctx["args"].reset_work:
        remove_owned_work(ctx)
    if work.exists():
        existing = read_json(marker, {}) if marker.exists() else {}
        if existing.get("taskId") != ctx["id"]:
            raise RuntimeError(f"拒绝接管无所有权标记的工作目录: {work}")
        if existing.get("sourceFingerprint") != fingerprint:
            raise RuntimeError("失败任务的来源已变化；确认后使用 --reset-work 重建工作区")
    else:
        work.mkdir(parents=True, mode=0o700)
        atomic_json(marker, {"taskId": ctx["id"], "title": ctx["title"], "sourceFingerprint": fingerprint, "createdAt": now()})
    os.chmod(work, 0o700)
    ctx["sourceRoot"].mkdir(parents=True, exist_ok=True, mode=0o700)
    ctx["outputRoot"].mkdir(parents=True, exist_ok=True, mode=0o700)
    require_mounted_volume(ctx["stateRoot"], "状态目录")
    ctx["stateRoot"].mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (ctx["sourceRoot"], ctx["outputRoot"]):
        os.chmod(path, 0o700)
    ensure_private_file(ctx["logPath"])


@contextlib.contextmanager
def task_lock(ctx: dict):
    path = ctx["stateRoot"] / f"{ctx['id']}.lock"
    require_mounted_volume(path, "状态目录")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(descriptor)
        raise RuntimeError(f"任务锁不安全: {path}")
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
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
    require_mounted_volume(ctx["stateRoot"], "状态目录")
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
        return f"{parsed.scheme}://{netloc}/…"
    return str(Path(source).expanduser())


# macOS 路径总长通常上限 1024 字节、单个组件上限 255 字节；两者都要检查，
# 否则 827 字节的 magnet 会被当成一个合法路径组件并在 stat 时触发 ENAMETOOLONG。
def is_plausible_path(value: str) -> bool:
    if urllib.parse.urlsplit(value).scheme.casefold() in {"magnet", "http", "https"}:
        return False
    return (
        "\x00" not in value
        and len(os.fsencode(value)) <= 1024
        and all(len(os.fsencode(part)) <= 255 for part in Path(value).parts)
    )


def validate_source(source: str, allow_local: bool = True) -> str:
    if not source or source != source.strip() or any(ord(char) < 32 for char in source):
        raise RuntimeError("来源包含不安全控制字符")
    if source.startswith("magnet:"):
        parsed = urllib.parse.urlsplit(source)
        if "xt=urn:btih:" not in parsed.query.casefold():
            raise RuntimeError("magnet 来源缺少 BTIH")
        return source
    local = Path(source).expanduser() if is_plausible_path(source) else None
    if allow_local and local is not None and local.exists():
        return source
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("来源只允许本地路径、magnet 或 HTTP(S)")
    return source


def signal_handler(_signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def scrub_source_text(text: str, source: str) -> str:
    text = text.replace(source, redacted_source(source))
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        parts = {parsed.query, urllib.parse.unquote(parsed.query), parsed.fragment, urllib.parse.unquote(parsed.fragment)}
        path = parsed.path.lstrip("/")
        if len(path) > 1:
            parts.update({path, urllib.parse.unquote(path)})
        parts.update(value for _key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if len(value) > 2)
        for part in sorted((part for part in parts if part), key=len, reverse=True):
            text = text.replace(part, "…")
        text = re.sub(r"https?://[^\s\"']+", lambda match: redacted_source(match.group(0)), text)
    return text


def scrub_log(path: Path, secrets: list[str]) -> None:
    secrets = [value for value in secrets if value]
    if not secrets or not path.exists():
        return
    temp = path.with_name(f".{path.name}.{os.getpid()}.scrub")
    with open(path, "r", encoding="utf-8", errors="replace") as source, open(temp, "w", encoding="utf-8") as target:
        for line in source:
            for secret in secrets:
                line = scrub_source_text(line, secret)
            target.write(line)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def run_child(ctx: dict, command: list[str], operation: str, timeout_seconds: int | None = None, redactions: list[str] | None = None) -> None:
    global ACTIVE_CHILD
    require_mounted_volume(ctx["stateRoot"], "状态目录")
    ensure_private_file(ctx["logPath"])
    status_update(ctx["id"], phase=operation, currentOperation=operation, childPid=None)
    code = -1
    try:
        with open(ctx["logPath"], "a", encoding="utf-8", errors="replace") as handle:
            ACTIVE_CHILD = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            status_update(ctx["id"], childPid=ACTIVE_CHILD.pid)
            started = time.monotonic()
            while ACTIVE_CHILD.poll() is None:
                if STOP_REQUESTED:
                    stop_child(ACTIVE_CHILD)
                    raise InterruptedError("任务已停止")
                if timeout_seconds and time.monotonic() - started > timeout_seconds:
                    stop_child(ACTIVE_CHILD)
                    raise TimeoutError(f"{operation} 超时")
                time.sleep(0.25)
            code = ACTIVE_CHILD.returncode
    finally:
        ACTIVE_CHILD = None
        scrub_log(ctx["logPath"], redactions or [])
    status_update(ctx["id"], childPid=None)
    if STOP_REQUESTED:
        raise InterruptedError("任务已停止")
    if code != 0:
        raise RuntimeError(f"{operation} 失败，退出码 {code}，日志: {ctx['logPath']}")


def stop_child(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def ytdlp_supports_no_remote_components() -> bool:
    # --no-remote-components 从 yt-dlp 2025.11 才有；旧版（含最后一个支持 Python 3.9 的 2025.10）不认识会 exit 2。
    # 旧版没有远程组件机制，不加该参数本身即安全。
    try:
        output = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        parts = output.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (int(parts[0]), int(parts[1])) >= (2025, 11)
    except Exception:
        pass
    return False


def ytdlp_auth_args(value: str | None) -> list[str]:
    if not value:
        return []
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise RuntimeError("--cookies 包含不安全控制字符")
    browser = re.split(r"[+:]", value, maxsplit=1)[0].casefold()
    if browser in YTDLP_BROWSERS:
        return ["--cookies-from-browser", value]
    expanded = os.path.expandvars(str(Path(value).expanduser()))
    path = Path(os.path.abspath(expanded))
    try:
        require_private_input(path, "--cookies 文件", 10 * 1024 * 1024)
    except OSError as exc:
        raise RuntimeError(f"--cookies 文件无法安全读取: {path}") from exc
    return ["--cookies", str(path)]


def ytdlp_base_command(cookies: str | None = None) -> list[str]:
    command = ["yt-dlp", "--ignore-config"]
    if ytdlp_supports_no_remote_components():
        command.append("--no-remote-components")
    return command + ytdlp_auth_args(cookies)


def validate_ytdlp_format(value: str | None) -> str | None:
    if value and (value != value.strip() or len(value) > 2048 or any(ord(char) < 32 for char in value)):
        raise RuntimeError("--format 无效")
    return value


def validate_subtitle_languages(value: str) -> str:
    codes = [item.strip() for item in str(value).split(",") if item.strip()]
    for code in codes:
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*", code):
            raise ValueError(f"字幕语言代码无效: {code}")
    return ",".join(codes)


def probe_summary(data: dict, playlist: bool) -> dict:
    if playlist:
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        return {
            "kind": "playlist",
            "id": data.get("id"),
            "title": data.get("title"),
            "entryCount": len(entries),
            "entries": [
                {
                    "index": item.get("playlist_index") or position,
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "duration": item.get("duration"),
                }
                for position, item in enumerate(entries, 1)
                if isinstance(item, dict)
            ],
        }
    formats = data.get("formats") if isinstance(data.get("formats"), list) else []
    return {
        "kind": "video",
        "id": data.get("id"),
        "title": data.get("title"),
        "duration": data.get("duration"),
        "extractor": data.get("extractor_key") or data.get("extractor"),
        "formats": [
            {
                key: item.get(key)
                for key in ("format_id", "ext", "resolution", "fps", "vcodec", "acodec", "filesize", "filesize_approx", "tbr", "protocol")
                if item.get(key) is not None
            }
            for item in formats
            if isinstance(item, dict)
        ],
    }


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


def torznab_search(name: str, source: dict, query: str, media_type: str, limit: int, timeout: int | None = None) -> list[dict]:
    key_env = str(source.get("apiKeyEnv", "JACKETT_API_KEY"))
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"搜索源 {name} 缺少环境变量 {key_env}")
    base = str(source.get("url", "")).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError(f"搜索源 {name} URL 无效")
    if source.get("type") == "jackett":
        indexer = urllib.parse.quote(str(source.get("indexer", "all")), safe="")
        url = f"{base}/api/v2.0/indexers/{indexer}/results/torznab/api"
    else:
        url = base
    search_type = "tvsearch" if media_type == "tv" else "movie"
    params = {"apikey": api_key, "t": search_type, "q": query}
    if source.get("categories"):
        params["cat"] = ",".join(str(value) for value in source["categories"])
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "media-downloader/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout or int(source.get("timeoutSeconds", 90))) as response:
            root = ET.fromstring(read_response(response, 10 * 1024 * 1024, f"Torznab {name}"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Torznab {name} 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Torznab {name} 连接失败: {exc.reason}") from exc
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
    if args.timeout is not None and not 1 <= args.timeout <= 300:
        raise ValueError("timeout 必须在 1-300 秒之间")
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
            if kind in {"jackett", "torznab"}:
                candidates.extend(torznab_search(name, source, args.query, args.media_type, limit, args.timeout))
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


def command_sources(_args) -> int:
    config = load_config()
    public = {}
    for name, source in config.get("searchSources", {}).items():
        public[name] = {key: value for key, value in source.items() if not is_sensitive_name(key)}
    print(json.dumps({"config": str(config_file()), "sources": public}, ensure_ascii=False, indent=2))
    return 0


def command_probe(args) -> int:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("网页媒体探测需要 yt-dlp；请先运行 doctor")
    if bool(args.source) == bool(args.source_file):
        raise RuntimeError("请且仅提供一个来源：位置参数或 --source-file")
    source = args.source
    if args.source_file:
        expanded = os.path.expandvars(str(Path(args.source_file).expanduser()))
        try:
            source = read_private_text(Path(os.path.abspath(expanded)))
        except OSError as exc:
            raise RuntimeError("--source-file 无法安全读取") from exc
    source = validate_source(source, allow_local=False)
    if not 1 <= args.timeout <= 300:
        raise RuntimeError("--timeout 必须在 1-300 秒之间")
    descriptor, batch_name = tempfile.mkstemp(prefix="agent-media-probe-", suffix=".txt")
    batch_path = Path(batch_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(source + "\n")
        command = ytdlp_base_command(args.cookies) + [
            "--simulate", "--dump-single-json",
            "--flat-playlist" if args.playlist else "--no-playlist",
            "--batch-file", str(batch_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"yt-dlp 探测超过 {args.timeout} 秒") from exc
    finally:
        batch_path.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = scrub_source_text((result.stderr or result.stdout or "yt-dlp 探测失败").strip()[-2000:], source)
        raise RuntimeError(detail)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp 返回了无效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("yt-dlp 返回的 JSON 结构无效")
    output = {"source": redacted_source(source), "authenticated": bool(args.cookies), **probe_summary(data, args.playlist)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def command_add_source(args) -> int:
    path = config_file()
    if not path.is_file():
        raise RuntimeError(f"配置不存在: {path}；请先复制 config.example.json")
    config = load_config()
    sources = config.setdefault("searchSources", {})
    if args.name in sources and not args.replace:
        raise RuntimeError(f"搜索源已存在: {args.name}；确认替换后使用 --replace")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.name):
        raise ValueError("来源名只能包含小写字母、数字、下划线和连字符")
    if args.kind == "web":
        source = {"type": "web", "enabled": True, "urlTemplate": validate_public_source_url(args.url, template=True)}
    else:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", args.api_key_env):
            raise ValueError("apiKeyEnv 必须是大写环境变量名")
        if not 1 <= args.timeout <= 300:
            raise ValueError("timeout 必须在 1-300 秒之间")
        source = {
            "type": "torznab", "enabled": True,
            "url": validate_public_source_url(args.url),
            "apiKeyEnv": args.api_key_env,
            "categories": args.category or [],
            "timeoutSeconds": args.timeout,
        }
    sources[args.name] = source
    validate_config(config)
    atomic_json(path, config, private=True)
    print(json.dumps({"saved": args.name, "config": str(path), "source": source}, ensure_ascii=False, indent=2))
    return 0


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
        expanded = os.path.expandvars(str(Path(args.source_file).expanduser()))
        path = Path(os.path.abspath(expanded))
        try:
            source = read_private_text(path)
        except OSError as exc:
            raise RuntimeError(f"--source-file 无法安全读取: {path}") from exc
    return validate_source(source), args.downloader


def classify_downloader(source: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if source.startswith("magnet:"):
        return "aria2"
    local = Path(source).expanduser() if is_plausible_path(source) else None
    if local is not None and local.exists():
        return "aria2" if local.suffix.lower() == ".torrent" else "local"
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"不支持的来源协议: {parsed.scheme or 'unknown'}")
    suffix = Path(parsed.path).suffix.lower()
    return "aria2" if suffix in VIDEO_EXTS | {".torrent"} else "yt-dlp"


def task_timeout_seconds(config: dict) -> int:
    return max(1, int(float(config.get("timeoutHours", 24)) * 3600))


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
        bt_stop_timeout = int(ctx["config"].get("btStopTimeoutSeconds", 600))
        if bt_stop_timeout:
            command.append(f"--bt-stop-timeout={bt_stop_timeout}")
        input_file = None
        local_source = Path(source).expanduser() if is_plausible_path(source) else None
        if local_source is not None and local_source.exists():
            command.append(str(local_source.resolve()))
        else:
            input_file = ctx["workRoot"] / ".aria2-input.txt"
            write_private_text(input_file, source + "\n")
            command.append(f"--input-file={input_file}")
        try:
            run_child(ctx, command, "downloading", task_timeout_seconds(ctx["config"]), [source])
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
        write_private_text(input_file, source + "\n")
        command = ytdlp_base_command(ctx["args"].cookies)
        selected_format = validate_ytdlp_format(ctx["args"].format)
        if selected_format:
            command += ["--format", selected_format]
        if ctx["args"].write_subs:
            language = (
                ctx["args"].sub_langs
                or ctx["config"].get("metadata", {}).get("subtitleLanguages")
                or ctx["config"].get("metadata", {}).get("language")
                or "zh-CN"
            )
            command += [
                "--write-subs", "--write-auto-subs",
                "--sub-format", "srt/best",
                "--sub-langs", validate_subtitle_languages(language),
            ]
        command += [
            playlist_flag, "--continue",
            "--no-overwrites", "--newline",
            "--paths", str(ctx["sourceRoot"]),
            "--output", "%(playlist_index,autonumber)03d %(title).180B [%(id)s].%(ext)s", "--batch-file", str(input_file),
        ]
        try:
            run_child(ctx, command, "downloading", task_timeout_seconds(ctx["config"]), [source])
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
MULTI_EPISODE_PATTERN = re.compile(r"(?i)S\d{1,2}[ ._-]*E\d{1,3}(?:[ ._-]*(?:-|E)E?\d{1,3}(?![0-9p]))+")


def apply_custom_words(config: dict, media_type: str, title: str) -> tuple[str, int]:
    """识别词预处理：屏蔽 → 替换 → 集数偏移（作用于识别前，不改用户标题存储）。"""
    words = config.get("customWords", {})
    for item in words.get("ignore", []):
        title = title.replace(item, " ")
    for item in words.get("replace", []):
        title = title.replace(item["from"], item["to"])
    offset = 0
    key = f"{media_type}:{title.casefold()}"
    for item in words.get("episodeOffset", []):
        if re.search(item["pattern"], key, re.IGNORECASE):
            offset = int(item.get("offset", 0))
    return re.sub(r"\s+", " ", title).strip(), offset


def episode_from_name(path: Path, default_season: int, offset: int = 0) -> tuple[int, int] | None:
    for text in (path.stem, path.parent.name):
        if MULTI_EPISODE_PATTERN.search(text):
            raise RuntimeError(f"暂不支持多集单文件: {path.name}；请先拆分为单集文件")
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(text)
            if match:
                season = int(match.groupdict().get("season") or default_season)
                episode = int(match.group("episode")) + offset
                if episode < 1:
                    raise RuntimeError(f"集数偏移后无效: {path.name} (E{int(match.group('episode'))}{offset:+d})")
                return season, episode
    return None


def ffprobe(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration,size", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe 读取超时: {path.name}") from exc
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


def resolved_episode_title(ctx: dict, source: Path, season: int, episode: int) -> str | None:
    details = next((
        item for item in ctx.get("metadata", {}).get("episodes", [])
        if item.get("season") == season and item.get("episode") == episode
    ), {})
    title = details.get("title")
    if not title and getattr(ctx["args"], "playlist", False):
        title = re.sub(r"^\d+\s+|\s+\[[^]]+\]$", "", source.stem)
    return sanitize_component(str(title)) if title else None


def planned_outputs(ctx: dict, sources: list[Path]) -> list[dict]:
    media_type = ctx["mediaType"]
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    plans = []
    seen = set()
    seen_episodes = set()
    for position, source in enumerate(sources, start=ctx["args"].episode or 1):
        source_info = validate_video(source, minimum)
        fields = {**ctx["namingFields"], "ext": source.suffix.lower().lstrip(".")} if ctx["args"].copy_original else ctx["namingFields"]
        if media_type == "movie":
            if len(sources) != 1:
                raise RuntimeError("电影任务必须明确提供单个主视频")
            relative = render_path(ctx["naming"]["movieFile"], fields)
            season = episode = None
        else:
            parsed = episode_from_name(source, ctx["args"].season, getattr(ctx["args"], "episode_offset", 0))
            if not parsed and getattr(ctx["args"], "playlist", False):
                parsed = (ctx["args"].season, position)
            if not parsed:
                if len(sources) == 1 and ctx["args"].episode is not None:
                    parsed = (ctx["args"].season, ctx["args"].episode)
                else:
                    raise RuntimeError(f"无法识别集号: {source.name}；请规范为 SxxExx/EPxx 或显式传 --season/--episode")
            season, episode = parsed
            if season < 0 or episode < 1:
                raise RuntimeError(f"季集号无效: S{season:02d}E{episode:02d}")
            if (season, episode) in seen_episodes:
                raise RuntimeError(f"多个来源映射到同一集: S{season:02d}E{episode:02d}")
            seen_episodes.add((season, episode))
            episode_title = resolved_episode_title(ctx, source, season, episode)
            episode_fields = {
                **fields,
                "season": season,
                "episode": episode,
                "episodeTitle": episode_title or "",
                "episodeTitleSuffix": f" - {episode_title}" if episode_title else "",
            }
            relative = render_path(ctx["naming"]["seasonDir"], episode_fields) / render_path(ctx["naming"]["episodeFile"], episode_fields)
        if relative in seen:
            raise RuntimeError(f"多个来源映射到同一输出: {relative}")
        seen.add(relative)
        stat = source.stat()
        plans.append({
            "source": source, "sourceInfo": source_info, "sourceSize": stat.st_size, "sourceMtimeNs": stat.st_mtime_ns,
            "relative": relative, "season": season, "episode": episode,
            "episodeTitle": episode_title if media_type == "tv" else None,
        })
    return plans


def ffmpeg_command(ctx: dict, source: Path, target: Path) -> list[str]:
    profile = ctx["profile"]
    codec = str(profile.get("videoCodec", "libx264"))
    audio_codec = str(profile.get("audioCodec", "aac"))
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-dn"]
    if profile["container"] == "mkv":
        # MKV 支持字幕流原样保留（不重编码）；MP4 兼容性差，继续丢弃内嵌字幕，外挂字幕由 copy_sidecars 保留。
        command += ["-map", "0:s?", "-c:s", "copy"]
    else:
        command += ["-sn"]
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


def copy_sidecars(plan: dict) -> None:
    output = plan["output"]
    for sidecar in plan["source"].parent.iterdir():
        if not sidecar.name.startswith(f"{plan['source'].stem}.") or not is_safe_file(plan["source"].parent, sidecar) or sidecar.suffix.lower() not in SUBTITLE_EXTS:
            continue
        tag = sidecar.name[len(plan["source"].stem):-len(sidecar.suffix)]
        shutil.copy2(sidecar, output.with_name(f"{output.stem}{tag}{sidecar.suffix.lower()}"))


def transcode(ctx: dict, plans: list[dict]) -> None:
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    for plan in plans:
        require_mounted_volume(ctx["baseRoot"], "工作目录")
        output = ctx["outputRoot"] / plan["relative"]
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(f".{output.stem}.partial{output.suffix}")
        temp.unlink(missing_ok=True)
        status_update(ctx["id"], phase="transcoding", currentFile=plan["source"].name)
        log(ctx, f"转码: {plan['source'].name} -> {plan['relative']}")
        run_child(ctx, ffmpeg_command(ctx, plan["source"], temp), "transcoding", task_timeout_seconds(ctx["config"]))
        source_stat = plan["source"].stat()
        if source_stat.st_size != plan["sourceSize"] or source_stat.st_mtime_ns != plan["sourceMtimeNs"]:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"转码期间来源仍在变化: {plan['source'].name}")
        output_info = validate_video(temp, minimum)
        if plan["sourceInfo"]["hasAudio"] and not output_info["hasAudio"]:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"转码输出缺少音频: {plan['source'].name}")
        allowed_shortfall = max(2.0, min(10.0, plan["sourceInfo"]["duration"] * 0.001))
        if output_info["duration"] + allowed_shortfall < plan["sourceInfo"]["duration"]:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"转码输出疑似截断: {plan['source'].name}")
        os.replace(temp, output)
        plan["output"] = output
        copy_sidecars(plan)


def organize(ctx: dict, plans: list[dict]) -> None:
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    for plan in plans:
        require_mounted_volume(ctx["baseRoot"], "工作目录")
        output = ctx["outputRoot"] / plan["relative"]
        output.parent.mkdir(parents=True, exist_ok=True)
        status_update(ctx["id"], phase="organizing", currentFile=plan["source"].name)
        log(ctx, f"整理原文件: {plan['source'].name} -> {plan['relative']}")
        atomic_copy(plan["source"], output, minimum)
        source_stat = plan["source"].stat()
        if source_stat.st_size != plan["sourceSize"] or source_stat.st_mtime_ns != plan["sourceMtimeNs"]:
            output.unlink(missing_ok=True)
            raise RuntimeError(f"整理期间来源仍在变化: {plan['source'].name}")
        plan["output"] = output
        copy_sidecars(plan)


def xml_text(parent: ET.Element, name: str, value) -> None:
    if value not in (None, "", []):
        ET.SubElement(parent, name).text = str(value)


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    ET.ElementTree(root).write(temp, encoding="utf-8", xml_declaration=True)
    os.replace(temp, path)


def episode_metadata(metadata: dict, season: int, episode: int) -> dict:
    return next((
        item for item in metadata.get("episodes", [])
        if item.get("season") == season and item.get("episode") == episode
    ), {})


def episode_nfo_root(season: int, episode: int, title: str | None, details: dict) -> ET.Element:
    root = ET.Element("episodedetails")
    xml_text(root, "title", title or f"Episode {episode}")
    xml_text(root, "season", season)
    xml_text(root, "episode", episode)
    xml_text(root, "aired", details.get("aired"))
    xml_text(root, "plot", details.get("plot"))
    xml_text(root, "runtime", details.get("runtime"))
    xml_text(root, "rating", details.get("rating"))
    for director in details.get("directors", []):
        xml_text(root, "director", director)
    for writer in details.get("writers", []):
        xml_text(root, "credits", writer)
    ids = details.get("ids", {}) if isinstance(details.get("ids"), dict) else {}
    for id_type, value in ids.items():
        if value:
            ET.SubElement(root, "uniqueid", {"type": str(id_type)}).text = str(value)
    return root


def write_nfo(ctx: dict, plans: list[dict]) -> None:
    require_mounted_volume(ctx["baseRoot"], "工作目录")
    metadata = ctx["metadata"]
    root_name = "tvshow" if ctx["mediaType"] == "tv" else "movie"
    root = ET.Element(root_name)
    xml_text(root, "title", metadata.get("title"))
    xml_text(root, "originaltitle", metadata.get("originalTitle"))
    xml_text(root, "sorttitle", metadata.get("sortTitle") or metadata.get("title"))
    xml_text(root, "year", metadata.get("year"))
    xml_text(root, "premiered", metadata.get("premiered"))
    xml_text(root, "plot", metadata.get("plot"))
    xml_text(root, "tagline", metadata.get("tagline"))
    xml_text(root, "mpaa", metadata.get("contentRating"))
    if metadata.get("rating") not in (None, ""):
        ratings = ET.SubElement(root, "ratings")
        source = metadata.get("ratingSource") or ("themoviedb" if metadata.get("ids", {}).get("tmdb") else "default")
        rating = ET.SubElement(ratings, "rating", {"name": str(source), "max": "10", "default": "true"})
        xml_text(rating, "value", metadata.get("rating"))
        xml_text(rating, "votes", metadata.get("ratingVotes"))
    xml_text(root, "runtime", metadata.get("runtime"))
    xml_text(root, "status", metadata.get("status"))
    xml_text(root, "studio", metadata.get("studio"))
    for genre in metadata.get("genres", []):
        xml_text(root, "genre", genre)
    for country in metadata.get("countries", []):
        xml_text(root, "country", country)
    for tag in metadata.get("tags", []):
        xml_text(root, "tag", tag)
    for director in metadata.get("directors", []):
        xml_text(root, "director", director)
    for writer in metadata.get("writers", []):
        xml_text(root, "credits", writer)
    for actor in metadata.get("actors", []):
        element = ET.SubElement(root, "actor")
        if isinstance(actor, str):
            xml_text(element, "name", actor)
        else:
            xml_text(element, "name", actor.get("name"))
            xml_text(element, "role", actor.get("role"))
            xml_text(element, "thumb", actor.get("thumb"))
    ids = metadata.get("ids", {}) if isinstance(metadata.get("ids"), dict) else {}
    first = True
    for id_type, value in ids.items():
        if value:
            element = ET.SubElement(root, "uniqueid", {"type": str(id_type), "default": "true" if first else "false"})
            element.text = str(value)
            first = False
    write_xml(ctx["outputRoot"] / f"{root_name}.nfo", root)
    if ctx["mediaType"] == "tv":
        for plan in plans:
            details = episode_metadata(metadata, plan["season"], plan["episode"])
            # 单集使用分集自身 ID；剧集 ID 只留在 tvshow.nfo，避免把整部剧误标成某一集。
            write_xml(
                plan["output"].with_suffix(".nfo"),
                episode_nfo_root(plan["season"], plan["episode"], plan.get("episodeTitle"), details),
            )


def download_image(source: str, destination: Path, ctx: dict) -> None:
    validate_source(source)
    temp_source = ctx["workRoot"] / f".artwork-source{Path(urllib.parse.urlsplit(source).path).suffix or '.img'}"
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "media-downloader/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response, open(temp_source, "wb") as handle:
                remaining = 25 * 1024 * 1024
                while remaining > 0:
                    if STOP_REQUESTED:
                        raise InterruptedError("任务已停止")
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
    temp_destination = destination.with_name(f".{destination.stem}.{os.getpid()}.partial{destination.suffix}")
    temp_destination.unlink(missing_ok=True)
    try:
        run_child(
            ctx,
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source_path), "-frames:v", "1", str(temp_destination)],
            "metadata",
            60,
        )
        if not temp_destination.is_file() or temp_destination.stat().st_size == 0:
            raise RuntimeError(f"图片转换失败: {source_path.name}")
        os.replace(temp_destination, destination)
    finally:
        temp_destination.unlink(missing_ok=True)
        temp_source.unlink(missing_ok=True)


def write_artwork(ctx: dict, plans: list[dict]) -> None:
    require_mounted_volume(ctx["baseRoot"], "工作目录")
    metadata = ctx["metadata"]
    roots = {ctx["sourceRoot"], *(plan["source"].parent for plan in plans)}
    source_images = {path for path in ctx["sourceRoot"].rglob("*") if is_safe_file(ctx["sourceRoot"], path) and path.suffix.lower() in IMAGE_EXTS}
    for root in roots - {ctx["sourceRoot"]}:
        source_images.update(path for path in root.iterdir() if is_safe_file(root, path) and path.suffix.lower() in IMAGE_EXTS)
    source_images = sorted(source_images)
    # ponytail: infer only conventional names; ambiguous libraries must provide metadata paths.
    poster = metadata.get("posterPath") or metadata.get("posterUrl") or next((str(path) for path in source_images if path.stem.casefold() in {"poster", "folder", "cover", "default", "movie"}), "")
    fanart = metadata.get("fanartPath") or metadata.get("fanartUrl") or next((str(path) for path in source_images if path.stem.casefold() in {"fanart", "backdrop", "background", "art"}), "")
    banner = metadata.get("bannerPath") or metadata.get("bannerUrl") or next((str(path) for path in source_images if path.stem.casefold() == "banner"), "")
    clearlogo = metadata.get("clearlogoPath") or metadata.get("clearlogoUrl") or next((str(path) for path in source_images if path.stem.casefold() in {"clearlogo", "logo"}), "")
    required = bool(ctx["config"].get("metadata", {}).get("requireArtwork", False))
    for kind, source, filename in (("poster", poster, "poster.jpg"), ("fanart", fanart, "fanart.jpg"), ("banner", banner, "banner.jpg"), ("clearlogo", clearlogo, "clearlogo.png")):
        if not source:
            if required and kind == "poster":
                raise RuntimeError("未找到必需的海报")
            continue
        try:
            download_image(str(source), ctx["outputRoot"] / filename, ctx)
        except InterruptedError:
            raise
        except Exception as exc:
            if required and kind == "poster":
                raise
            log(ctx, f"警告: {kind} 获取失败: {exc}")
    # Plex/Kodi 单集缩略图：优先 metadata 提供的 thumbPath/thumbUrl，其次 TMDB 分集 still_path（thumbUrl）。
    if ctx["mediaType"] == "tv":
        for plan in plans:
            details = episode_metadata(metadata, plan["season"], plan["episode"])
            thumb = details.get("thumbPath") or details.get("thumbUrl")
            if not thumb:
                continue
            try:
                download_image(str(thumb), plan["output"].parent / f"{plan['output'].stem}-thumb.jpg", ctx)
            except InterruptedError:
                raise
            except Exception as exc:
                log(ctx, f"警告: S{plan['season']:02d}E{plan['episode']:02d} 剧照获取失败: {exc}")


def missing_episode_report(ctx: dict, plans: list[dict]) -> str | None:
    """剧集缺集检查：对本次覆盖到的季，按已知集数范围找空洞。只报告，不阻断。"""
    if ctx["mediaType"] != "tv" or ctx["args"].playlist:
        return None
    known = {(item["season"], item["episode"]) for item in ctx["metadata"].get("episodes", [])}
    by_season: dict[int, set[int]] = {}
    for plan in plans:
        if plan["season"] is not None:
            by_season.setdefault(plan["season"], set()).add(plan["episode"])
    missing: dict[str, list[int]] = {}
    for season, episodes in sorted(by_season.items()):
        candidates = {episode for s, episode in known if s == season} or set(range(1, max(episodes) + 1))
        gaps = sorted(candidates - episodes)
        if gaps:
            missing[f"S{season:02d}"] = gaps
    return missing or None


def repair_root(value: str, apply: bool) -> Path:
    raw = Path(os.path.expandvars(value)).expanduser()
    if raw.is_symlink():
        raise RuntimeError(f"修复目录不得是符号链接: {raw}")
    root = raw.resolve()
    if root in {Path("/"), Path.home().resolve(), Path("/Volumes")} or not root.is_dir():
        raise RuntimeError(f"请指定一个已存在的单部剧目录: {root}")
    require_mounted_volume(root, "修复目录")
    if apply and not os.access(root, os.W_OK):
        raise RuntimeError(f"修复目录不可写: {root}")
    return root


def repair_sidecars(root: Path, video: Path) -> list[Path]:
    return sorted(
        path for path in video.parent.iterdir()
        if path != video
        and path.name.startswith(f"{video.stem}.")
        and path.suffix.lower() in REPAIR_SIDECAR_EXTS
        and is_safe_file(root, path)
    )


def build_repair_plan(config: dict, args) -> dict:
    if args.season < 0:
        raise ValueError("--season 不得小于 0")
    root = repair_root(args.root, args.apply)
    metadata = resolve_metadata(config, args)
    _profile_name, profile = select_profile(config, "tv", args.profile)
    naming_name, naming = select_naming(config, profile, "tv", args.naming)
    canonical = canonical_name(metadata.get("title") or args.title, metadata.get("year"))
    videos = media_files(root)
    if not videos:
        raise RuntimeError(f"修复目录没有媒体文件: {root}")
    looks_like_show = (root / "tvshow.nfo").is_file() or any(
        len(path.relative_to(root).parts) == 1
        or re.fullmatch(r"(?i)(?:season|s)\s*\d+|第\s*\d+\s*季", path.relative_to(root).parts[0])
        for path in videos
    )
    if not looks_like_show:
        raise RuntimeError(f"修复只接受单部剧目录，不能直接传媒体库或分类根目录: {root}")

    base_fields = {
        "title": sanitize_component(metadata.get("title") or args.title),
        "canonical": canonical,
        "year": metadata.get("year") or "",
    }
    seen_episodes = set()
    seen_sources = set()
    seen_targets = set()
    moves = []
    nfo_updates = []
    items = []
    for video in videos:
        parsed = episode_from_name(video, args.season, getattr(args, "episode_offset", 0))
        if not parsed:
            items.append({"source": str(video), "action": "keep", "reason": "unrecognized-episode"})
            continue
        season, episode = parsed
        if (season, episode) in seen_episodes:
            raise RuntimeError(f"同一季集存在多个媒体文件，拒绝修复: S{season:02d}E{episode:02d}")
        seen_episodes.add((season, episode))
        details = episode_metadata(metadata, season, episode)
        raw_title = details.get("title")
        if not raw_title:
            items.append({
                "source": str(video), "action": "keep", "reason": "no-reliable-title",
                "season": season, "episode": episode,
            })
            continue
        episode_title = sanitize_component(str(raw_title))
        fields = {
            **base_fields,
            "season": season,
            "episode": episode,
            "episodeTitle": episode_title,
            "episodeTitleSuffix": f" - {episode_title}",
            "ext": video.suffix.lower().lstrip("."),
        }
        relative = render_path(naming["seasonDir"], fields) / render_path(naming["episodeFile"], fields)
        target_video = root / relative
        if not target_video.resolve(strict=False).is_relative_to(root):
            raise RuntimeError(f"修复目标逃逸: {target_video}")
        action = "keep" if target_video == video else "rename"
        items.append({
            "source": str(video), "target": str(target_video), "action": action,
            "season": season, "episode": episode, "title": episode_title,
        })
        for source in [video, *repair_sidecars(root, video)]:
            if source == video:
                target = target_video
            else:
                tag = source.name[len(video.stem):-len(source.suffix)]
                target = target_video.with_name(f"{target_video.stem}{tag}{source.suffix.lower()}")
            if source == target:
                continue
            if source in seen_sources or target in seen_targets:
                raise RuntimeError(f"修复映射重复: {source} -> {target}")
            if target.exists():
                raise RuntimeError(f"修复目标已存在，拒绝覆盖: {target}")
            stat_info = source.stat()
            seen_sources.add(source)
            seen_targets.add(target)
            moves.append({
                "source": source, "target": target,
                "identity": (stat_info.st_dev, stat_info.st_ino, stat_info.st_size, stat_info.st_mtime_ns),
            })
        if args.update_nfo:
            nfo_target = target_video.with_suffix(".nfo")
            if nfo_target.is_symlink():
                raise RuntimeError(f"NFO 目标不安全: {nfo_target}")
            nfo_updates.append({
                "target": nfo_target,
                "root": episode_nfo_root(season, episode, episode_title, details),
            })

    return {
        "version": VERSION,
        "configSchemaVersion": CONFIG_SCHEMA_VERSION,
        "mode": "apply" if args.apply else "preview",
        "root": root,
        "rootIdentity": directory_identity(root),
        "canonical": canonical,
        "naming": naming_name,
        "moves": moves,
        "nfoUpdates": nfo_updates,
        "items": items,
    }


def apply_repair_plan(config: dict, plan: dict) -> None:
    root = plan["root"]
    state_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_STATE_DIR") or config.get("stateDir") or (RUNTIME_DIR / "repair"))
    require_work_root(state_root, "状态目录")
    lock_name = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    with json_lock(state_root / f"repair-{lock_name}.json"):
        require_mounted_volume(root, "修复目录")
        if directory_identity(root) != plan["rootIdentity"]:
            raise RuntimeError(f"修复目录在预览后发生变化: {root}")
        for move in plan["moves"]:
            source, target = move["source"], move["target"]
            if not is_safe_file(root, source):
                raise RuntimeError(f"修复来源不安全或已变化: {source}")
            stat_info = source.stat()
            identity = (stat_info.st_dev, stat_info.st_ino, stat_info.st_size, stat_info.st_mtime_ns)
            if identity != move["identity"]:
                raise RuntimeError(f"修复来源在预览后发生变化: {source}")
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"修复目标已存在，拒绝覆盖: {target}")
            if not target.resolve(strict=False).is_relative_to(root):
                raise RuntimeError(f"修复目标逃逸: {target}")

        for move in plan["moves"]:
            target = move["target"]
            ensure_target_parent({"targetRoot": root, "targetIdentity": plan["rootIdentity"]}, target.parent)
            try:
                os.link(move["source"], target, follow_symlinks=False)
            except FileExistsError as exc:
                raise RuntimeError(f"修复目标已存在，拒绝覆盖: {target}") from exc
            except OSError as exc:
                if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                    raise
                # ponytail: SMB and some filesystems lack hard links; verified copy is the safe fallback.
                atomic_copy(move["source"], target, 0)

        if directory_identity(root) != plan["rootIdentity"]:
            raise RuntimeError(f"修复目录在执行期间发生变化: {root}")
        for move in plan["moves"]:
            source, target = move["source"], move["target"]
            if not existing_matches(source, target, 0):
                raise RuntimeError(f"修复副本校验失败，保留原文件: {target}")
        # 所有新路径都完成逐字节校验后才移除旧路径；中途失败最多留下副本，不会丢失媒体。
        for move in plan["moves"]:
            move["source"].unlink()

        for update in plan["nfoUpdates"]:
            target = update["target"]
            if not target.resolve(strict=False).is_relative_to(root):
                raise RuntimeError(f"NFO 目标逃逸: {target}")
            if target.is_symlink():
                raise RuntimeError(f"NFO 目标不安全: {target}")
            ensure_target_parent({"targetRoot": root, "targetIdentity": plan["rootIdentity"]}, target.parent)
            descriptor, temp_name = tempfile.mkstemp(prefix="repair-nfo-", suffix=".nfo", dir=state_root)
            os.close(descriptor)
            temp = Path(temp_name)
            try:
                write_xml(temp, update["root"])
                if target.exists():
                    atomic_replace_nfo(temp, target)
                else:
                    atomic_copy(temp, target, 0)
            finally:
                temp.unlink(missing_ok=True)


def command_repair(args) -> int:
    config = load_config()
    plan = build_repair_plan(config, args)
    if args.apply:
        apply_repair_plan(config, plan)
    output = {
        key: value for key, value in plan.items()
        if key not in {"rootIdentity", "moves", "nfoUpdates"}
    }
    output["root"] = str(plan["root"])
    output["renameCount"] = len(plan["moves"])
    output["nfoUpdateCount"] = len(plan["nfoUpdates"])
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if STOP_REQUESTED:
                raise InterruptedError("任务已停止")
            digest.update(chunk)
    return digest.hexdigest()


def existing_matches(source: Path, target: Path, minimum_duration: float) -> bool:
    if target.is_symlink() or not target.is_file() or source.stat().st_size != target.stat().st_size:
        return False
    if source.suffix.lower() in VIDEO_EXTS:
        try:
            validate_video(target, minimum_duration)
        except RuntimeError:
            return False
    return file_digest(source) == file_digest(target)


def atomic_copy(source: Path, target: Path, minimum_duration: float) -> None:
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise RuntimeError(f"归档目录不可用: {target.parent}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
    temp = Path(temp_name)
    try:
        with open(source, "rb") as src, os.fdopen(descriptor, "wb") as dst:
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
                    current = target.lstat()
                    if created_identity == (current.st_dev, current.st_ino):
                        target.unlink()
                raise
    finally:
        temp.unlink(missing_ok=True)


def atomic_replace_nfo(source: Path, target: Path) -> None:
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"NFO 目标不安全: {target}")
    identity = directory_identity(target)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
    temp = Path(temp_name)
    try:
        with open(source, "rb") as src, os.fdopen(descriptor, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, temp)
        if file_digest(source) != file_digest(temp):
            raise RuntimeError(f"NFO 复制哈希不一致: {target}")
        if directory_identity(target) != identity:
            raise RuntimeError(f"NFO 在更新期间发生变化: {target}")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def archive_existing_action(ctx: dict, source: Path, target: Path, relative: Path, minimum: float) -> str:
    if target.is_symlink():
        raise RuntimeError(f"目标是符号链接，拒绝归档: {target}")
    if not target.exists():
        return "copy"
    if existing_matches(source, target, minimum):
        return "keep"
    if source.suffix.lower() == ".nfo" and ctx["args"].update_nfo:
        return "replace"
    if (
        ctx["args"].merge
        and ctx["mediaType"] == "tv"
        and len(relative.parts) == 1
        and relative.name.casefold() in TV_SHARED_MERGE_FILES
    ):
        return "skip"
    raise RuntimeError(f"目标已存在且内容不同，拒绝覆盖: {target}")


def reject_existing_episode_alias(ctx: dict, target: Path, relative: Path) -> None:
    if ctx["mediaType"] != "tv" or relative.suffix.lower() not in VIDEO_EXTS or not target.parent.is_dir():
        return
    marker = re.search(r"(?i)S\d{2}E\d{2,3}", relative.name)
    if not marker:
        return
    for existing in target.parent.iterdir():
        existing_marker = re.search(r"(?i)S\d{2}E\d{2,3}", existing.name)
        if (
            existing != target
            and existing.is_file()
            and not existing.is_symlink()
            and existing.suffix.lower() in VIDEO_EXTS
            and existing_marker
            and existing_marker.group().casefold() == marker.group().casefold()
        ):
            raise RuntimeError(f"同一季集已存在，拒绝创建重复文件: {existing}")


def archive(ctx: dict, phase: str = "archiving", action: str = "归档") -> list[str]:
    minimum = float(ctx["config"].get("minMediaDurationSeconds", 120))
    output_root = ctx["outputRoot"].resolve()
    files = sorted(
        path for path in output_root.rglob("*")
        if is_safe_file(output_root, path)
        and path.suffix.lower() in ARCHIVE_EXTS
        and not any(part.startswith(".") for part in path.relative_to(output_root).parts)
    )
    if not files:
        raise RuntimeError("没有可归档文件")
    prepared = []
    for source in files:
        relative = source.relative_to(output_root)
        target = ctx["targetShow"] / relative
        if not target.resolve(strict=False).is_relative_to(ctx["targetRoot"]):
            raise RuntimeError("归档目标逃逸")
        reject_existing_episode_alias(ctx, target, relative)
        prepared.append((source, relative, target, archive_existing_action(ctx, source, target, relative, minimum)))
    archived = []
    for source, relative, target, prepared_action in prepared:
        if STOP_REQUESTED:
            raise InterruptedError("任务已停止")
        ensure_target_parent(ctx, target.parent)
        current_action = archive_existing_action(ctx, source, target, relative, minimum)
        if current_action == "replace":
            log(ctx, f"更新 NFO: {relative}")
            atomic_replace_nfo(source, target)
        elif current_action == "skip":
            log(ctx, f"合并跳过已有共享文件: {relative}")
        elif current_action == "copy":
            log(ctx, f"{action}: {relative}")
            copied = True
            try:
                atomic_copy(source, target, minimum)
            except RuntimeError:
                race_action = archive_existing_action(ctx, source, target, relative, minimum)
                if race_action not in {"keep", "skip"}:
                    raise
                copied = False
                log(ctx, f"并发合并保留已有共享文件: {relative}" if race_action == "skip" else f"其他任务已写入相同文件: {relative}")
            if copied and (not target.is_file() or source.stat().st_size != target.stat().st_size):
                raise RuntimeError(f"{action}校验失败: {target}")
        elif prepared_action == "copy" and current_action == "keep":
            log(ctx, f"其他任务已写入相同文件: {relative}")
        else:
            assert current_action == "keep"
        archived.append(str(target))
        status_update(ctx["id"], phase=phase, currentFile=str(relative), archivedFiles=archived)
    require_target_root(ctx["targetRoot"])
    if directory_identity(ctx["targetRoot"]) != ctx["targetIdentity"]:
        raise RuntimeError(f"目标目录在任务期间发生变化: {ctx['targetRoot']}")
    return archived


def pipeline(args) -> int:
    config = load_config()
    if args.no_deliver:
        args.no_archive = True
    if args.copy_original is None:
        args.copy_original = config.get("defaultModes", {}).get(args.media_type, "transcode") == "organize"
    ctx = build_context(config, args)
    source, requested_downloader = resolve_source(args)
    downloader = classify_downloader(source, requested_downloader)
    if args.playlist and args.media_type != "tv":
        raise RuntimeError("--playlist 必须使用 --type tv，才能按播放顺序映射季集")
    if (args.format or args.cookies or args.write_subs) and downloader != "yt-dlp":
        raise RuntimeError("--format/--cookies/--write-subs 只适用于 yt-dlp 网页来源")
    if downloader == "yt-dlp":
        validate_ytdlp_format(args.format)
        ytdlp_auth_args(args.cookies)
    plan = {
        "version": VERSION, "configSchemaVersion": CONFIG_SCHEMA_VERSION,
        "taskId": ctx["id"], "title": ctx["canonical"], "mediaType": ctx["mediaType"],
        "requestedTitle": args.title,
        "profile": ctx["profileName"], "target": ctx["targetName"],
        "naming": ctx["namingName"],
        "mode": "organize" if args.copy_original else "transcode",
        "archive": not args.no_archive,
        "merge": args.merge,
        "playlist": args.playlist,
        "format": args.format or "best available",
        "subs": bool(args.write_subs),
        "authenticated": bool(args.cookies),
        "downloader": downloader,
        "targetPath": str(ctx["targetShow"]), "source": redacted_source(source),
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    with task_lock(ctx):
        ensure_work(ctx, source)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        status_update(
            ctx["id"], **plan, phase="starting", pid=os.getpid(), childPid=None,
            currentOperation="starting", currentFile="", startedAt=now(), finishedAt=None,
            lastError="", archivedFiles=[], logPath=str(ctx["logPath"]), workPath=str(ctx["workRoot"]),
        )
        try:
            sources = acquire(ctx, source, requested_downloader)
            plans = planned_outputs(ctx, sources)
            (organize if args.copy_original else transcode)(ctx, plans)
            status_update(ctx["id"], phase="metadata", currentOperation="metadata", currentFile="")
            write_nfo(ctx, plans)
            write_artwork(ctx, plans)
            missing = missing_episode_report(ctx, plans)
            if missing:
                gaps = ", ".join(f"{season} 缺 {','.join(f'E{episode:02d}' for episode in episodes)}" for season, episodes in missing.items())
                log(ctx, f"缺集提醒: {gaps}")
                print(f"缺集提醒: {gaps}", file=sys.stderr)
            if args.no_archive:
                if ctx["downloadRoot"] is not None:
                    status_update(ctx["id"], phase="delivering", currentOperation="delivering")
                    archived = archive(ctx, phase="delivering", action="转移到下载目录")
                else:
                    archived = [str(path) for path in sorted(ctx["outputRoot"].rglob("*")) if path.is_file()]
            else:
                status_update(ctx["id"], phase="archiving", currentOperation="archiving")
                archived = archive(ctx)
            # no-archive 配了 downloadDir 时成品已移走，工作区（含下载缓存）也可安全清理
            if not args.keep_work and (not args.no_archive or ctx["downloadRoot"] is not None):
                remove_owned_work(ctx)
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
    print(json.dumps({"defaultModes": config.get("defaultModes", {"tv": "transcode", "movie": "transcode"}), "defaultProfiles": config.get("defaultProfiles", {}), "defaultNaming": config.get("defaultNaming", "plex"), "downloadDir": str(configured_download_root(config) or ""), "profiles": config.get("profiles", {}), "targets": config.get("targets", {}), "namingPresets": config.get("namingPresets", {})}, ensure_ascii=False, indent=2))
    return 0


def command_check(args) -> int:
    states = status_read()
    if args.title:
        title = args.title.casefold()
        states = {
            key: value for key, value in states.items()
            if any(title in str(value.get(name, "")).casefold() for name in ("title", "requestedTitle"))
        }
    print(json.dumps(states, ensure_ascii=False, indent=2))
    return 0 if states else 1


def process_matches(pid: int, title: str) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.returncode == 0 and "media-downloader.py" in result.stdout and title in result.stdout


def owned_child_matches(pid: int, work_path: str) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "pgid=,command="], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return False
    fields = result.stdout.strip().split(maxsplit=1)
    if len(fields) != 2 or not fields[0].isdigit():
        return False
    executables = {os.path.basename(token) for token in fields[1].split()[:3]}
    return int(fields[0]) == pid and bool(executables & {"aria2c", "ffmpeg", "yt-dlp"}) and work_path in fields[1]


def signal_owned_child(pid: int, work_path: str, sig: signal.Signals) -> None:
    if owned_child_matches(pid, work_path):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, sig)


def command_stop(args) -> int:
    config = load_config()
    base_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_BASE_DIR") or config.get("baseDir") or (SKILL_DIR / "work"))
    matches = []
    for identifier, state in status_read().items():
        names = {str(state.get("title", "")).casefold(), str(state.get("requestedTitle", "")).casefold()}
        if args.title.casefold() in names and state.get("phase") not in {"done", "failed", "stopped"}:
            matches.append((identifier, state))
    if not matches:
        print("未找到运行中的任务")
        return 1
    for identifier, state in matches:
        pid = state.get("pid")
        child_pid = state.get("childPid")
        process_title = str(state.get("requestedTitle") or state.get("title", ""))
        work_path = str(state.get("workPath") or (base_root / ".media-downloader-work" / identifier))
        if isinstance(pid, int) and process_matches(pid, process_title):
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"已发送停止信号: {state.get('title')} pid={pid}")
            except ProcessLookupError:
                pass
            grace = time.monotonic() + 2
            while time.monotonic() < grace and process_matches(pid, process_title):
                time.sleep(0.1)
        if isinstance(child_pid, int):
            signal_owned_child(child_pid, work_path, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            parent_running = isinstance(pid, int) and process_matches(pid, process_title)
            child_running = isinstance(child_pid, int) and owned_child_matches(child_pid, work_path)
            if not parent_running and not child_running:
                break
            time.sleep(0.1)
        if isinstance(child_pid, int):
            signal_owned_child(child_pid, work_path, signal.SIGKILL)
        if isinstance(pid, int) and process_matches(pid, process_title):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        final_deadline = time.monotonic() + 2
        while time.monotonic() < final_deadline:
            parent_running = isinstance(pid, int) and process_matches(pid, process_title)
            child_running = isinstance(child_pid, int) and owned_child_matches(child_pid, work_path)
            if not parent_running and not child_running:
                break
            time.sleep(0.1)
        if (isinstance(pid, int) and process_matches(pid, process_title)) or (isinstance(child_pid, int) and owned_child_matches(child_pid, work_path)):
            raise RuntimeError(f"停止任务失败，进程仍在运行: {state.get('title')}")
        status_update(identifier, phase="stopped", currentOperation="stopped", lastError="任务已停止", pid=None, childPid=None, finishedAt=now())
    return 0


def command_doctor(_args) -> int:
    config = load_config()
    checks = []
    for tool, required in (("ffmpeg", True), ("ffprobe", True), ("aria2c", False), ("yt-dlp", False)):
        location = shutil.which(tool)
        checks.append({"name": tool, "status": "ok" if location else ("error" if required else "optional-missing"), "path": location or ""})
    base_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_BASE_DIR") or config.get("baseDir") or (SKILL_DIR / "work"))
    state_root = resolve_path(os.environ.get("MEDIA_DOWNLOADER_STATE_DIR") or config.get("stateDir") or (base_root / ".state"))
    for name, path, label in (("work:base", base_root, "工作目录"), ("work:state", state_root, "状态目录")):
        try:
            require_work_root(path, label)
            status = "ok"
        except RuntimeError:
            status = "error"
        checks.append({"name": name, "status": status, "path": str(path)})
    download_root = configured_download_root(config)
    if download_root is not None:
        try:
            validate_download_root(base_root, download_root)
            status = "ok"
        except RuntimeError:
            status = "unavailable"
        checks.append({"name": "download:output", "status": status, "path": str(download_root)})
    for name, raw in (config.get("targets", {}) or {}).items():
        value = raw.get("path") if isinstance(raw, dict) else raw
        path = resolve_path(value)
        try:
            require_target_root(path)
            status = "ok"
        except RuntimeError:
            status = "unavailable"
        checks.append({"name": f"target:{name}", "status": status, "path": str(path)})
    if os.environ.get("MEDIA_DOWNLOADER_TARGET_DIR"):
        path = resolve_path(os.environ["MEDIA_DOWNLOADER_TARGET_DIR"])
        try:
            require_target_root(path)
            status = "ok"
        except RuntimeError:
            status = "unavailable"
        checks.append({"name": "target:environment", "status": status, "path": str(path)})
    for name, source in (config.get("searchSources", {}) or {}).items():
        if isinstance(source, dict) and source.get("type") in {"jackett", "torznab"} and source.get("enabled", True):
            env_name = str(source.get("apiKeyEnv", "JACKETT_API_KEY"))
            checks.append({"name": f"search:{name}", "status": "ok" if os.environ.get(env_name) else "optional-missing", "detail": env_name})
    metadata = config.get("metadata", {})
    if metadata.get("provider") == "tmdb":
        env_name = str(metadata.get("apiKeyEnv", "TMDB_API_KEY"))
        checks.append({"name": "metadata:tmdb", "status": "ok" if os.environ.get(env_name) else "optional-missing", "detail": env_name})
    print(json.dumps({"version": VERSION, "configSchemaVersion": CONFIG_SCHEMA_VERSION, "config": str(config_file()), "checks": checks}, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] == "error" for item in checks) else 0


def add_pipeline_arguments(parser: argparse.ArgumentParser, source_required: bool = True, mode_override: bool = True) -> None:
    parser.add_argument("title")
    parser.add_argument("source", nargs=None if source_required else "?")
    parser.add_argument("--candidate")
    parser.add_argument("--source-file", help="A plain file owned by the current user with no group/other access, holding exactly one source")
    parser.add_argument("--type", dest="media_type", choices=("tv", "movie"), default="tv")
    parser.add_argument("--year", type=int)
    parser.add_argument("--profile")
    parser.add_argument("--target")
    parser.add_argument("--naming")
    parser.add_argument("--metadata", help="Agent-provided metadata JSON")
    parser.add_argument("--downloader", choices=("auto", "aria2", "yt-dlp", "local"), default="auto")
    parser.add_argument("--season", type=int, default=1)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--playlist", action="store_true", help="Explicitly download a whole YouTube/Bilibili playlist; TV maps episodes in playlist order")
    parser.add_argument("--format", help="yt-dlp format selector (e.g. bv*[height<=720]+ba/b[height<=720])")
    parser.add_argument("--cookies", help="Browser name for --cookies-from-browser (e.g. chrome) or path to a cookies.txt for --cookies")
    parser.add_argument("--write-subs", action="store_true", help="Download external subtitles when the yt-dlp source provides them; unavailable subtitles are skipped")
    parser.add_argument("--sub-langs", help="Comma-separated subtitle language codes (e.g. zh-CN,en); defaults to the metadata language or zh-CN")
    parser.add_argument("--no-archive", action="store_true", help="Skip archive transfer; deliver to downloadDir if configured, else keep output in the workspace")
    parser.add_argument("--no-deliver", action="store_true", help="Do not archive or deliver; keep Plex-ready output in the owned workspace")
    parser.add_argument("--keep-work", action="store_true", help="Keep the download cache/workspace after completion (cleaned by default after delivery/archive)")
    parser.add_argument("--reset-work", action="store_true")
    parser.add_argument("--update-nfo", action="store_true", help="Atomically update existing NFO only; never overwrites media, subtitles, or artwork")
    parser.add_argument("--merge", action="store_true", help="For incremental TV archives, keep existing different show-level artwork/tvshow.nfo; media conflicts still fail")
    if mode_override:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--transcode", dest="copy_original", action="store_false", help="Override the default mode and transcode")
        mode.add_argument("--no-transcode", dest="copy_original", action="store_true", help="Override the default mode, keep the original container, and organize only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.set_defaults(handler=pipeline, copy_original=None)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Agent media ingest pipeline")
    root.add_argument("--version", action="version", version=f"Agent Media Pipeline {VERSION} (config schema {CONFIG_SCHEMA_VERSION})")
    commands = root.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Search configured structured sources and cache candidates")
    search.add_argument("query")
    search.add_argument("--source", action="append")
    search.add_argument("--type", dest="media_type", choices=("tv", "movie"), default="tv")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--timeout", type=int, help="Override this search request timeout in seconds (1-300)")
    search.set_defaults(handler=command_search)
    sources = commands.add_parser("sources", help="List search sources in the private config")
    sources.set_defaults(handler=command_sources)
    probe = commands.add_parser("probe", help="Inspect a YouTube/Bilibili video or playlist and list available quality formats")
    probe.add_argument("source", nargs="?")
    probe.add_argument("--source-file", help="Private 0600 file holding exactly one URL")
    probe.add_argument("--playlist", action="store_true", help="Inspect playlist structure and ordering instead of one video")
    probe.add_argument("--cookies", help="Browser name or private cookies.txt path")
    probe.add_argument("--timeout", type=int, default=120)
    probe.set_defaults(handler=command_probe)
    add_source = commands.add_parser("add-source", help="Add a user-confirmed search source to private config.json")
    add_source.add_argument("name")
    add_source.add_argument("url")
    add_source.add_argument("--type", dest="kind", choices=("web", "torznab"), required=True)
    add_source.add_argument("--api-key-env", default="TORZNAB_API_KEY")
    add_source.add_argument("--category", action="append")
    add_source.add_argument("--timeout", type=int, default=30)
    add_source.add_argument("--replace", action="store_true")
    add_source.set_defaults(handler=command_add_source)
    repair = commands.add_parser("repair", help="Preview or apply safe in-place TV episode naming/NFO repair")
    repair.add_argument("title")
    repair.add_argument("root", help="Exact existing TV show folder; never pass a whole library root")
    repair.add_argument("--year", type=int)
    repair.add_argument("--season", type=int, default=1, help="Season to query from the metadata provider; Season 0 is supported")
    repair.add_argument("--profile")
    repair.add_argument("--naming")
    repair.add_argument("--metadata", help="Agent-provided metadata JSON")
    repair.add_argument("--offline", action="store_true")
    repair.add_argument("--update-nfo", action="store_true", help="Update per-episode NFO only when reliable episode metadata is available")
    repair.add_argument("--apply", action="store_true", help="Apply the displayed repair plan; preview is the default")
    repair.set_defaults(handler=command_repair, media_type="tv")
    for name in ("ingest", "resume", "download"):
        add_pipeline_arguments(commands.add_parser(name, help="Acquire, transcode, organize, and archive/deliver"), source_required=False)
    for name in ("adopt", "process"):
        add_pipeline_arguments(commands.add_parser(name, help="Process local media and archive/deliver"), source_required=True)
    organize_parser = commands.add_parser("organize", help="Organize local media without transcoding (keep container) and archive/deliver")
    add_pipeline_arguments(organize_parser, source_required=True, mode_override=False)
    organize_parser.set_defaults(copy_original=True)
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
