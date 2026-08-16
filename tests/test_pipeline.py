#!/usr/bin/env python3
"""One dependency-free integration check for the full media ingest contract."""

from __future__ import annotations

import contextlib
import fcntl
import http.server
import importlib.util
import json
import os
import shutil
import signal
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "media-downloader.py"


def run(command, *, env=None, expect=0, cwd=PROJECT):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != expect:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expect}: {command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def make_video(path: Path, seconds: int = 3):
    path.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=1000",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path),
    ])


def make_subtitled_video(path: Path, seconds: int = 3):
    path.parent.mkdir(parents=True, exist_ok=True)
    subtitle = path.with_name(f".{path.stem}.srt")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\n保留字幕\n", encoding="utf-8")
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=1000",
        "-i", str(subtitle),
        "-t", str(seconds), "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt",
        str(path),
    ])
    subtitle.unlink()


def make_image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=80x120", "-frames:v", "1", str(path)])


class Handler(http.server.SimpleHTTPRequestHandler):
    root: Path
    last_query = {}

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.endswith("/torznab/api"):
            type(self).last_query = urllib.parse.parse_qs(parsed.query)
            url = f"http://127.0.0.1:{self.server.server_address[1]}/Remote.S01E01.mp4"
            payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
<title>Remote S01E01 1080p</title><pubDate>Tue, 12 Aug 2026 00:00:00 GMT</pubDate>
<enclosure url="{url}" length="1" type="application/x-bittorrent" />
<torznab:attr name="seeders" value="42"/><torznab:attr name="size" value="123456"/>
</item></channel></rss>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def translate_path(self, path):
        relative = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip("/")
        return str(self.root / relative)


@contextlib.contextmanager
def server(root: Path):
    Handler.root = root
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def config(root: Path, port: int) -> Path:
    data = {
        "baseDir": str(root / "work"),
        "stateDir": str(root / "state"),
        "timeoutHours": 1,
        "minMediaDurationSeconds": 1,
        "defaultProfiles": {"tv": "tv", "movie": "movie"},
        "defaultModes": {"tv": "transcode", "movie": "transcode"},
        "defaultNaming": "plex",
        "targets": {"tv": {"path": str(root / "tv")}, "movie": {"path": str(root / "movie")}},
        "namingPresets": {
            "plex": {
                "tv": {"showDir": "{canonical}", "seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}.{ext}"},
                "movie": {"showDir": "{canonical}", "movieFile": "{canonical}.{ext}"},
            },
            "plex-title": {
                "tv": {"showDir": "{canonical}", "seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}{episodeTitleSuffix}.{ext}"},
                "movie": {"showDir": "{canonical}", "movieFile": "{canonical}.{ext}"},
            },
        },
        "profiles": {
            "tv": {"type": "tv", "container": "mp4", "resolution": 90, "videoCodec": "libx264", "crf": 28, "audioCodec": "aac", "audioBitrate": "64k", "preset": "ultrafast", "target": "tv"},
            "movie": {"type": "movie", "container": "mp4", "resolution": 90, "videoCodec": "libx264", "crf": 28, "audioCodec": "aac", "audioBitrate": "64k", "preset": "ultrafast", "target": "movie"},
            "movie-mkv": {"type": "movie", "container": "mkv", "resolution": 90, "videoCodec": "libx264", "crf": 28, "audioCodec": "aac", "audioBitrate": "64k", "preset": "ultrafast", "target": "movie"},
        },
        "searchSources": {
            "jackett": {"type": "jackett", "enabled": True, "url": f"http://127.0.0.1:{port}", "indexer": "all", "apiKeyEnv": "TEST_JACKETT_KEY"},
            "prowlarr": {"type": "torznab", "enabled": False, "url": f"http://127.0.0.1:{port}/torznab/api", "apiKeyEnv": "TEST_TORZNAB_KEY"},
            "web": {"type": "web", "enabled": True, "urlTemplate": "https://example.test/search?q={query}"},
        },
        "metadata": {"provider": "none", "tvFallback": "none", "requireArtwork": False},
    }
    path = root / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def assert_xml(path: Path, expected: str):
    text = path.read_text(encoding="utf-8")
    assert expected in text, (path, text)


def assert_atomic_copy_never_overwrites(root: Path):
    spec = importlib.util.spec_from_file_location("media_downloader", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = root / "atomic-source.txt"
    target = root / "atomic-target.txt"
    source.write_text("source", encoding="utf-8")
    original_link = module.os.link

    def racing_link(source_path, target_path):
        Path(target_path).write_text("other task", encoding="utf-8")
        return original_link(source_path, target_path)

    module.os.link = racing_link
    try:
        try:
            module.atomic_copy(source, target, 0)
        except RuntimeError as exc:
            assert "拒绝覆盖" in str(exc)
        else:
            raise AssertionError("concurrent target creation must fail")
    finally:
        module.os.link = original_link
    assert target.read_text(encoding="utf-8") == "other task"

    fallback = root / "atomic-fallback.txt"

    def unsupported_link(_source_path, _target_path, **_kwargs):
        raise OSError(module.errno.EOPNOTSUPP, "hard links unavailable")

    module.os.link = unsupported_link
    try:
        module.atomic_copy(source, fallback, 0)
    finally:
        module.os.link = original_link
    assert fallback.read_text(encoding="utf-8") == "source"

    failed_fallback = root / "atomic-failed-fallback.txt"
    original_digest = module.file_digest
    calls = 0

    def fail_after_target_created(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("simulated verification failure")
        return original_digest(path)

    module.os.link = unsupported_link
    module.file_digest = fail_after_target_created
    try:
        try:
            module.atomic_copy(source, failed_fallback, 0)
        except RuntimeError as exc:
            assert "simulated" in str(exc)
        else:
            raise AssertionError("failed fallback copies must fail")
    finally:
        module.os.link = original_link
        module.file_digest = original_digest
    assert not failed_fallback.exists()

    repair_root = (root / "repair-copy-fallback").resolve()
    repair_root.mkdir()
    repair_source = repair_root / "old.srt"
    repair_target = repair_root / "new.srt"
    repair_source.write_text("subtitle", encoding="utf-8")
    repair_stat = repair_source.stat()
    module.os.link = unsupported_link
    try:
        module.apply_repair_plan({"stateDir": str(root / "repair-state")}, {
            "root": repair_root,
            "rootIdentity": module.directory_identity(repair_root),
            "moves": [{
                "source": repair_source,
                "target": repair_target,
                "identity": (repair_stat.st_dev, repair_stat.st_ino, repair_stat.st_size, repair_stat.st_mtime_ns),
            }],
            "nfoUpdates": [],
        })
    finally:
        module.os.link = original_link
    assert repair_target.read_text(encoding="utf-8") == "subtitle" and not repair_source.exists()

    symlink_target = root / "atomic-symlink.txt"
    symlink_target.symlink_to(source)
    assert not module.existing_matches(source, symlink_target, 0)
    return module


def assert_tmdb_auth_modes():
    spec = importlib.util.spec_from_file_location("media_downloader_tmdb", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls = []

    def fake_http_json(url, params, headers=None, timeout=20):
        calls.append((url, dict(params), dict(headers or {})))
        if "/search/" in url:
            return {"results": [{"id": 42, "name": "Stub", "first_air_date": "2020-01-01"}]}
        if "/season/" in url:
            return {"episodes": [{
                "id": 999, "season_number": 2, "episode_number": 3, "name": "第三集",
                "overview": "剧情", "air_date": "2020-01-03", "runtime": 45, "vote_average": 8.2,
                "still_path": "/stub-still.jpg",
            }]}
        return {"id": 42, "name": "Stub", "first_air_date": "2020-01-01"}

    original = module.http_json
    module.http_json = fake_http_json
    try:
        config = {"metadata": {"apiKeyEnv": "TEST_TMDB_AUTH_KEY"}}
        # v4 JWT（eyJ 开头）必须走 Bearer header，且不出现 api_key 参数
        os.environ["TEST_TMDB_AUTH_KEY"] = "eyJhbGciOiJIUzI1NiJ9.stub.sig"
        fetched = module.fetch_tmdb(config, "tv", "Stub", None, 2)
        assert calls[0][2].get("Authorization") == "Bearer eyJhbGciOiJIUzI1NiJ9.stub.sig", calls
        assert "api_key" not in calls[0][1], calls
        assert calls[1][2].get("Authorization") == "Bearer eyJhbGciOiJIUzI1NiJ9.stub.sig", calls
        assert calls[2][2].get("Authorization") == "Bearer eyJhbGciOiJIUzI1NiJ9.stub.sig", calls
        assert fetched["episodes"][0]["title"] == "第三集"
        assert fetched["episodes"][0]["ids"]["tmdb"] == 999
        assert fetched["episodes"][0]["thumbUrl"].endswith("/stub-still.jpg")
        calls.clear()
        # v3 key 保持 api_key 参数，不带 Authorization header
        os.environ["TEST_TMDB_AUTH_KEY"] = "v3-api-key-123"
        module.fetch_tmdb(config, "tv", "Stub", None, 2)
        assert calls[0][1].get("api_key") == "v3-api-key-123", calls
        assert not calls[0][2].get("Authorization"), calls
        assert calls[1][1].get("api_key") == "v3-api-key-123", calls
    finally:
        module.http_json = original
        os.environ.pop("TEST_TMDB_AUTH_KEY", None)


def assert_path_and_naming_guards(module, root: Path):
    assert module.episode_from_name(Path("Show.S01E01-1080p.mkv"), 1) == (1, 1)
    assert module.episode_from_name(Path("Show.S01E01-720p.mkv"), 1) == (1, 1)
    try:
        module.episode_from_name(Path("Show.S01E01-E02.mkv"), 1)
    except RuntimeError as exc:
        assert "多集单文件" in str(exc)
    else:
        raise AssertionError("multi-episode files must be rejected")

    target = root / "identity-target"
    target.mkdir()
    ctx = {"targetRoot": target, "targetIdentity": module.directory_identity(target)}
    target.rename(root / "identity-target-old")
    target.mkdir()
    try:
        module.ensure_target_parent(ctx, target / "Movie")
    except RuntimeError as exc:
        assert "发生变化" in str(exc)
    else:
        raise AssertionError("replaced target roots must be rejected")

    episode_dir = root / "episode-alias" / "Season 02"
    episode_dir.mkdir(parents=True)
    existing_episode = episode_dir / "Show - S02E03 - Old title.mkv"
    existing_episode.write_bytes(b"old")
    try:
        module.reject_existing_episode_alias(
            {"mediaType": "tv"}, episode_dir / "Show - S02E03 - New title.mkv", Path("Season 02/Show - S02E03 - New title.mkv")
        )
    except RuntimeError as exc:
        assert "同一季集已存在" in str(exc)
    else:
        raise AssertionError("different names for the same episode must not create duplicates")

    canonical_id = module.task_id("movie", "测试电影 (2025)", root / "movie")
    assert canonical_id == module.task_id("movie", "测试电影 (2025)", root / "movie")
    assert canonical_id != module.task_id("movie", "测试电影 (2025)", root / "other-movie")
    ctx_a = {"mediaType": "tv", "canonical": "测试剧 (2026)", "targetRoot": root / "tv"}
    id_magnet_a = module.pipeline_task_id(ctx_a, "magnet:?xt=urn:btih:AAAA")
    id_magnet_b = module.pipeline_task_id(ctx_a, "magnet:?xt=urn:btih:BBBB")
    assert id_magnet_a != id_magnet_b
    assert id_magnet_a == module.pipeline_task_id(ctx_a, "magnet:?xt=urn:btih:AAAA")
    command = module.ffmpeg_command({"profile": {"container": "mp4", "videoCodec": "libx264", "audioCodec": "aac", "resolution": 720}}, Path("input.mkv"), Path("output.mp4"))
    assert command[command.index("-map_metadata") + 1] == "-1"
    assert command[command.index("-map_chapters") + 1] == "-1"
    assert "-sn" in command and "-c:s" not in command
    mkv_command = module.ffmpeg_command({"profile": {"container": "mkv", "videoCodec": "libx264", "audioCodec": "aac", "resolution": 720}}, Path("input.mkv"), Path("output.mkv"))
    assert "-sn" not in mkv_command
    assert mkv_command[mkv_command.index("0:s?") - 1] == "-map"
    assert mkv_command[mkv_command.index("-c:s") + 1] == "copy"
    playlist_args = type("Args", (), {"copy_original": True, "season": 3, "episode": 5, "playlist": True})()
    playlist_root = root / "playlist"
    playlist_root.mkdir()
    playlist_files = [playlist_root / "001 开场 [a].mkv", playlist_root / "002 进阶 [b].mkv"]
    for path in playlist_files:
        path.write_bytes(b"x")
    original_validate = module.validate_video
    module.validate_video = lambda _path, _minimum: {"duration": 1, "hasVideo": True, "hasAudio": False}
    try:
        plans = module.planned_outputs({
            "mediaType": "tv", "config": {"minMediaDurationSeconds": 0}, "args": playlist_args,
            "namingFields": {"canonical": "课程", "ext": "mkv"},
            "naming": {"seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}{episodeTitleSuffix}.{ext}"},
        }, playlist_files)
        assert [(plan["season"], plan["episode"]) for plan in plans] == [(3, 5), (3, 6)]
        assert plans[0]["relative"].name == "课程 - S03E05 - 开场.mkv"
        titled = playlist_root / "Show.S02E03.mkv"
        untitled = playlist_root / "Show.S02E04.mkv"
        titled.write_bytes(b"x")
        untitled.write_bytes(b"x")
        naming = {"seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}{episodeTitleSuffix}.{ext}"}
        titled_plan = module.planned_outputs({
            "mediaType": "tv", "config": {"minMediaDurationSeconds": 0},
            "args": type("Args", (), {"copy_original": True, "season": 1, "episode": None, "playlist": False})(),
            "metadata": {"episodes": [{"season": 2, "episode": 3, "title": "单集/标题"}]},
            "namingFields": {"canonical": "剧名", "ext": "mkv"}, "naming": naming,
        }, [titled])[0]
        assert titled_plan["relative"].name == "剧名 - S02E03 - 单集 标题.mkv"
        untitled_plan = module.planned_outputs({
            "mediaType": "tv", "config": {"minMediaDurationSeconds": 0},
            "args": type("Args", (), {"copy_original": True, "season": 1, "episode": None, "playlist": False})(),
            "metadata": {"episodes": []}, "namingFields": {"canonical": "剧名", "ext": "mkv"}, "naming": naming,
        }, [untitled])[0]
        assert untitled_plan["relative"].name == "剧名 - S02E04.mkv"
    finally:
        module.validate_video = original_validate
    try:
        module.sanitize_component("剧" * 100)
    except ValueError as exc:
        assert "字节" in str(exc)
    else:
        raise AssertionError("overlong UTF-8 path components must be rejected")

    # 超长 magnet（827 字符）不得被当成本地路径 stat 而炸 ENAMETOOLONG
    long_magnet = "magnet:?xt=urn:btih:ABCDEF0123456789&dn=" + "x" * 780
    assert len(long_magnet) > 255
    assert module.validate_source(long_magnet) == long_magnet
    assert module.classify_downloader(long_magnet, "auto") == "aria2"
    assert module.is_plausible_path(long_magnet) is False
    assert module.is_plausible_path("https://example.test/video.mp4") is False
    signed = "https://example.test/private/video?token=DO_NOT_LEAK&expires=123"
    scrubbed = module.scrub_source_text(f"failed private/video?token=DO_NOT_LEAK&expires=123 from {signed}", signed)
    assert "DO_NOT_LEAK" not in scrubbed and "expires=123" not in scrubbed, scrubbed
    # 回归实际 acquire 分支：长 magnet 必须写入 aria2 input-file，不能进入 Path.exists()。
    magnet_work = root / "magnet-work"
    magnet_source = magnet_work / "source"
    magnet_source.mkdir(parents=True)
    fake_media = magnet_source / "result.mkv"
    fake_media.write_bytes(b"x")
    captured = []
    original_log, original_run_child, original_which = module.log, module.run_child, module.shutil.which
    try:
        module.log = lambda *_args: None
        module.shutil.which = lambda name: f"/fake/{name}"
        module.run_child = lambda _ctx, command, *_args: captured.append(command)
        acquired = module.acquire({
            "sourceRoot": magnet_source,
            "workRoot": magnet_work,
            "config": {"timeoutHours": 1},
            "args": type("Args", (), {"playlist": False})(),
        }, long_magnet, "auto")
        assert acquired == [fake_media]
        assert any(arg.startswith("--input-file=") for arg in captured[0]), captured
        assert "--bt-stop-timeout=600" in captured[0], captured
    finally:
        module.log, module.run_child, module.shutil.which = original_log, original_run_child, original_which

    # yt-dlp：格式、登录、播放列表顺序和输出模板必须显式且可审计。
    cookie_file = root / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie_file.chmod(0o600)
    assert module.ytdlp_auth_args("chrome") == ["--cookies-from-browser", "chrome"]
    assert module.ytdlp_auth_args("whale:Default") == ["--cookies-from-browser", "whale:Default"]
    assert module.ytdlp_auth_args(str(cookie_file)) == ["--cookies", str(cookie_file)]
    cookie_file.chmod(0o644)
    try:
        module.ytdlp_auth_args(str(cookie_file))
    except RuntimeError as exc:
        assert "--cookies 文件：必须" in str(exc)
    else:
        raise AssertionError("insecure cookies files must be rejected")
    cookie_file.chmod(0o600)
    try:
        module.validate_ytdlp_format("best\n--exec=x")
    except RuntimeError as exc:
        assert "--format 无效" in str(exc)
    else:
        raise AssertionError("yt-dlp format control characters must be rejected")
    try:
        module.validate_subtitle_languages("zh-CN,en")
        module.validate_subtitle_languages("zh-CN\n--exec=x")
    except ValueError as exc:
        assert "字幕语言代码无效" in str(exc)
    else:
        raise AssertionError("invalid subtitle language codes must be rejected")
    playlist_probe = module.probe_summary({"id": "p", "title": "Playlist", "entries": [
        {"id": "a", "title": "One", "playlist_index": 1},
        {"id": "b", "title": "Two", "playlist_index": 2},
    ]}, True)
    assert playlist_probe["entryCount"] == 2
    assert [item["index"] for item in playlist_probe["entries"]] == [1, 2]
    web_work = root / "yt-work"
    web_source = web_work / "source"
    web_source.mkdir(parents=True)
    web_media = web_source / "001 Example [id].mp4"
    web_media.write_bytes(b"x")
    captured = []
    original_log, original_run_child = module.log, module.run_child
    original_which, original_remote_check = module.shutil.which, module.ytdlp_supports_no_remote_components
    try:
        module.log = lambda *_args: None
        module.run_child = lambda _ctx, command, *_args: captured.append(command)
        module.shutil.which = lambda name: f"/fake/{name}"
        module.ytdlp_supports_no_remote_components = lambda: True
        acquired = module.acquire({
            "sourceRoot": web_source,
            "workRoot": web_work,
            "config": {"timeoutHours": 1},
            "args": type("Args", (), {"playlist": True, "format": "bv*[height<=720]+ba/b", "cookies": "chrome", "write_subs": True, "sub_langs": "zh-CN,en"})(),
        }, "https://example.test/playlist", "yt-dlp")
        assert acquired == [web_media]
        command = captured[0]
        assert command[command.index("--output") + 1].startswith("%(playlist_index,autonumber)03d"), command
        assert command[command.index("--format") + 1] == "bv*[height<=720]+ba/b", command
        assert command[command.index("--cookies-from-browser") + 1] == "chrome", command
        assert "--yes-playlist" in command
        assert "--write-subs" in command and "--write-auto-subs" in command
        assert command[command.index("--sub-format") + 1] == "srt/best"
        assert command[command.index("--sub-langs") + 1] == "zh-CN,en"
    finally:
        module.log, module.run_child = original_log, original_run_child
        module.shutil.which, module.ytdlp_supports_no_remote_components = original_which, original_remote_check

    # customWords：屏蔽/替换作用于检索词；偏移按 media_type+清洗后标题匹配
    words_config = {"customWords": {
        "ignore": ["全39集"],
        "replace": [{"from": "第12话", "to": "E12"}],
        "episodeOffset": [{"pattern": r"(?i)tv:续作", "offset": 50}],
    }}
    cleaned, offset = module.apply_custom_words(words_config, "tv", "狂飙 全39集")
    assert cleaned == "狂飙" and offset == 0, (cleaned, offset)
    cleaned, _ = module.apply_custom_words(words_config, "tv", "某剧 第12话")
    assert cleaned == "某剧 E12", cleaned
    cleaned, offset = module.apply_custom_words(words_config, "tv", "续作 第二季")
    assert offset == 50, (cleaned, offset)
    # 偏移作用于集号；movie 不匹配 tv 前缀的 pattern
    assert module.episode_from_name(Path("续作.E05.mkv"), 1, 50) == (1, 55)
    _, offset = module.apply_custom_words(words_config, "movie", "续作 第二季")
    assert offset == 0
    try:
        module.episode_from_name(Path("续作.E05.mkv"), 1, -10)
    except RuntimeError as exc:
        assert "偏移" in str(exc)
    else:
        raise AssertionError("offset producing episode < 1 must be rejected")

    # 缺集报告：空洞检测 + 不计更大集号
    report_ctx = {
        "mediaType": "tv",
        "args": type("Args", (), {"playlist": False})(),
        "metadata": {"episodes": []},
    }
    plans = [{"season": 1, "episode": 1}, {"season": 1, "episode": 3}, {"season": 1, "episode": 4}]
    assert module.missing_episode_report(report_ctx, plans) == {"S01": [2]}
    assert module.missing_episode_report(report_ctx, [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}]) is None
    report_ctx["metadata"]["episodes"] = [{"season": 1, "episode": 5}, {"season": 1, "episode": 6}]
    assert module.missing_episode_report(report_ctx, [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}]) == {"S01": [5, 6]}
    report_ctx["args"] = type("Args", (), {"playlist": True})()
    assert module.missing_episode_report(report_ctx, plans) is None

    # 两个分集任务同时争写共享 fanart 时，--merge 的后写者保留先写结果并继续。
    race_target_root = (root / "merge-race-target").resolve()
    race_target_show = race_target_root / "Show"
    race_output = root / "merge-race-output"
    race_target_show.mkdir(parents=True)
    race_output.mkdir()
    (race_output / "fanart.jpg").write_bytes(b"incoming")
    race_logs = []
    original_atomic, original_log, original_status = module.atomic_copy, module.log, module.status_update
    try:
        def race_atomic(_source, target, _minimum):
            target.write_bytes(b"other task")
            raise RuntimeError("simulated concurrent writer")

        module.atomic_copy = race_atomic
        module.log = lambda _ctx, message: race_logs.append(message)
        module.status_update = lambda *_args, **_kwargs: {}
        archived = module.archive({
            "outputRoot": race_output,
            "targetRoot": race_target_root,
            "targetIdentity": module.directory_identity(race_target_root),
            "targetShow": race_target_show,
            "mediaType": "tv",
            "config": {"minMediaDurationSeconds": 0},
            "args": type("Args", (), {"merge": True, "update_nfo": False})(),
            "id": "merge-race",
        })
        assert archived == [str(race_target_show / "fanart.jpg")]
        assert (race_target_show / "fanart.jpg").read_bytes() == b"other task"
        assert any("并发合并" in message for message in race_logs), race_logs
    finally:
        module.atomic_copy, module.log, module.status_update = original_atomic, original_log, original_status

    args = type("Args", (), {"copy_original": True, "season": 1, "episode": None})()
    duplicate_root = root / "duplicate-episodes"
    duplicate_root.mkdir()
    first = duplicate_root / "Show.S01E01.mkv"
    second = duplicate_root / "Show.S01E01.mp4"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    original_validate = module.validate_video
    module.validate_video = lambda _path, _minimum: {"duration": 1, "hasVideo": True, "hasAudio": False}
    try:
        # 转码模式：同集不同容器映射到同一输出
        try:
            module.planned_outputs({
                "mediaType": "tv", "config": {"minMediaDurationSeconds": 0}, "args": args,
                "namingFields": {"canonical": "Show", "ext": "mp4"},
                "naming": {"seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}.{ext}"},
            }, [first, second])
        except RuntimeError as exc:
            assert "同一集" in str(exc)
        else:
            raise AssertionError("duplicate episodes with different containers must be rejected")
        # 免转码模式：同集不同容器保留原名会互相覆盖
        try:
            module.planned_outputs({
                "mediaType": "tv", "config": {"minMediaDurationSeconds": 0}, "args": args,
                "namingFields": {"canonical": "Show", "ext": "{sourceExt}"},
                "naming": {"seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}.{ext}"},
            }, [first, second])
        except RuntimeError as exc:
            assert "同一集" in str(exc)
        else:
            raise AssertionError("organize mode must reject duplicate episodes in different containers")
    finally:
        module.validate_video = original_validate


def main():
    with tempfile.TemporaryDirectory(prefix="media-downloader-test.") as temp:
        root = Path(temp)
        version = run([sys.executable, str(SCRIPT), "--version"])
        assert version.stdout.strip() == "Agent Media Pipeline 0.4.0 (config schema 1)"
        module = assert_atomic_copy_never_overwrites(root)
        assert_path_and_naming_guards(module, root)
        assert_tmdb_auth_modes()
        for name in ("tv", "movie", "source", "http"):
            (root / name).mkdir()
        make_video(root / "source" / "Example.S02E03.mkv")
        make_video(root / "source" / "Film.mkv")
        make_video(root / "source" / "Organize.Show.S04E05.mkv")
        make_video(root / "source" / "Organize.Movie.mkv")
        make_subtitled_video(root / "source" / "Subtitled.Movie.mkv")
        make_video(root / "http" / "Remote.S01E01.mp4")
        make_image(root / "source" / "poster.png")
        (root / "source" / "Example.S02E03.zh.forced.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
        metadata = root / "metadata.json"
        metadata.write_text(json.dumps({
            "title": "示例剧", "originalTitle": "Example Show", "year": 2026,
            "plot": "测试剧情", "ids": {"tmdb": 123}, "posterPath": str(root / "source" / "poster.png"),
            "episodes": [{"season": 2, "episode": 3, "title": "第三集", "plot": "单集剧情", "ids": {"tmdb": 999}}],
        }, ensure_ascii=False), encoding="utf-8")
        movie_metadata = root / "movie-metadata.json"
        movie_metadata.write_text(json.dumps({
            "title": "测试电影", "originalTitle": "Test Film", "year": 2025,
            "sortTitle": "Test Film, The", "plot": "电影剧情", "tagline": "电影标语",
            "rating": 7.5, "ratingVotes": 1550, "ratingSource": "themoviedb",
            "countries": ["中国"], "tags": ["测试"], "directors": ["测试导演"],
            "writers": ["测试编剧"], "actors": [{"name": "测试演员", "role": "角色"}],
            "posterPath": str(root / "source" / "poster.png"),
            "bannerPath": str(root / "source" / "poster.png"),
            "clearlogoPath": str(root / "source" / "poster.png"),
        }, ensure_ascii=False), encoding="utf-8")

        with server(root / "http") as port:
            cfg = config(root, port)
            env = {**os.environ, "MEDIA_DOWNLOADER_CONFIG": str(cfg), "MEDIA_DOWNLOADER_STATUS_FILE": str(root / "status.json"), "MEDIA_DOWNLOADER_CANDIDATE_FILE": str(root / "candidates.json"), "MEDIA_DOWNLOADER_OFFLINE": "1", "TEST_JACKETT_KEY": "secret-key", "TEST_TORZNAB_KEY": "secret-key"}

            (root / "movie").rmdir()
            doctor = run([sys.executable, str(SCRIPT), "doctor"], env=env)
            doctor_payload = json.loads(doctor.stdout)
            assert doctor_payload["version"] == "0.4.0"
            assert doctor_payload["configSchemaVersion"] == 1
            checks = {item["name"]: item["status"] for item in doctor_payload["checks"]}
            assert checks["work:base"] == "ok"
            assert checks["work:state"] == "ok"
            assert checks["target:tv"] == "ok"
            assert checks["target:movie"] == "unavailable"
            optional_env = dict(env)
            optional_env.pop("TEST_JACKETT_KEY", None)
            optional_doctor = run([sys.executable, str(SCRIPT), "doctor"], env=optional_env)
            optional_checks = {item["name"]: item["status"] for item in json.loads(optional_doctor.stdout)["checks"]}
            assert optional_checks["search:jackett"] == "optional-missing"
            (root / "movie").mkdir()
            env_target = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_TARGET_DIR": str(root / "tv")})
            env_checks = {item["name"]: item["status"] for item in json.loads(env_target.stdout)["checks"]}
            assert env_checks["target:environment"] == "ok"

            modes = json.loads(cfg.read_text(encoding="utf-8"))
            modes["defaultModes"] = {"tv": "organize", "movie": "transcode"}
            modes_cfg = root / "modes.json"
            modes_cfg.write_text(json.dumps(modes), encoding="utf-8")
            modes_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(modes_cfg)}
            tv_default = run([sys.executable, str(SCRIPT), "adopt", "Mode TV", str(root / "source" / "Example.S02E03.mkv"), "--type", "tv", "--target", "tv", "--offline", "--dry-run"], env=modes_env)
            assert json.loads(tv_default.stdout)["mode"] == "organize"
            tv_override = run([sys.executable, str(SCRIPT), "adopt", "Mode TV", str(root / "source" / "Example.S02E03.mkv"), "--type", "tv", "--target", "tv", "--offline", "--transcode", "--dry-run"], env=modes_env)
            assert json.loads(tv_override.stdout)["mode"] == "transcode"
            movie_default = run([sys.executable, str(SCRIPT), "adopt", "Mode Movie", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--offline", "--dry-run"], env=modes_env)
            assert json.loads(movie_default.stdout)["mode"] == "transcode"
            movie_override = run([sys.executable, str(SCRIPT), "adopt", "Mode Movie", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--offline", "--no-transcode", "--dry-run"], env=modes_env)
            assert json.loads(movie_override.stdout)["mode"] == "organize"
            modes.pop("defaultModes")
            legacy_cfg = root / "legacy-modes.json"
            legacy_cfg.write_text(json.dumps(modes), encoding="utf-8")
            legacy_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(legacy_cfg)}
            legacy_default = run([sys.executable, str(SCRIPT), "adopt", "Legacy Mode", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--offline", "--dry-run"], env=legacy_env)
            assert json.loads(legacy_default.stdout)["mode"] == "transcode"
            profiles = run([sys.executable, str(SCRIPT), "profiles"], env=modes_env)
            assert json.loads(profiles.stdout)["defaultModes"] == {"tv": "organize", "movie": "transcode"}

            source_cfg = root / "source-config.json"
            source_cfg.write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
            source_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(source_cfg)}
            added_web = run([sys.executable, str(SCRIPT), "add-source", "public_web", "https://example.test/search?q={query}", "--type", "web"], env=source_env)
            assert json.loads(added_web.stdout)["saved"] == "public_web"
            assert stat.S_IMODE(source_cfg.stat().st_mode) == 0o600
            listed = json.loads(run([sys.executable, str(SCRIPT), "sources"], env=source_env).stdout)
            assert listed["sources"]["public_web"]["urlTemplate"].endswith("{query}")
            duplicate = run([sys.executable, str(SCRIPT), "add-source", "public_web", "https://example.test/find?q={query}", "--type", "web"], env=source_env, expect=1)
            assert "--replace" in duplicate.stderr
            secret_url = run([sys.executable, str(SCRIPT), "add-source", "unsafe", "https://example.test/api?apikey=secret", "--type", "torznab"], env=source_env, expect=1)
            assert "不得内嵌" in secret_url.stderr
            for index, key in enumerate(("access_token", "auth_token", "password", "client_secret", "X-Amz-Signature"), start=1):
                secret_url = run([sys.executable, str(SCRIPT), "add-source", f"unsafe{index}", f"https://example.test/api?{key}=secret", "--type", "torznab"], env=source_env, expect=1)
                assert "不得内嵌" in secret_url.stderr

            broken_work = json.loads(cfg.read_text(encoding="utf-8"))
            broken_work["baseDir"] = "/"
            broken_work_cfg = root / "broken-work.json"
            broken_work_cfg.write_text(json.dumps(broken_work), encoding="utf-8")
            broken_doctor = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_CONFIG": str(broken_work_cfg)}, expect=1)
            broken_checks = {item["name"]: item["status"] for item in json.loads(broken_doctor.stdout)["checks"]}
            assert broken_checks["work:base"] == "error"

            # 未配置 downloadDir 时，no-archive 向后兼容：成品留在受控工作区
            no_archive_config = json.loads(cfg.read_text(encoding="utf-8"))
            no_archive_config["targets"] = {}
            no_archive_cfg = root / "no-archive.json"
            no_archive_cfg.write_text(json.dumps(no_archive_config), encoding="utf-8")
            no_archive_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(no_archive_cfg)}
            no_archive_command = [sys.executable, str(SCRIPT), "adopt", "仅处理电影", str(root / "source" / "Film.mkv"), "--type", "movie", "--no-transcode", "--no-archive", "--offline"]
            no_archive_plan = json.loads(run([*no_archive_command, "--dry-run"], env=no_archive_env).stdout)
            assert no_archive_plan["archive"] is False
            run(no_archive_command, env=no_archive_env)
            local_output = Path(no_archive_plan["targetPath"])
            assert (local_output / "仅处理电影.mkv").is_file()
            assert (local_output / "movie.nfo").is_file()

            # downloadDir 可位于 baseDir 内；交付经完整校验后清理任务工作区
            delivery_config = json.loads(no_archive_cfg.read_text(encoding="utf-8"))
            delivery_root = root / "work" / "Incoming"
            delivery_config["downloadDir"] = str(delivery_root)
            delivery_cfg = root / "delivery.json"
            delivery_cfg.write_text(json.dumps(delivery_config), encoding="utf-8")
            delivery_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(delivery_cfg)}
            delivery_checks = {item["name"]: item["status"] for item in json.loads(run([sys.executable, str(SCRIPT), "doctor"], env=delivery_env).stdout)["checks"]}
            assert delivery_checks["download:output"] == "ok"
            delivery_url = f"http://127.0.0.1:{port}/Remote.S01E01.mp4"
            delivery_command = [sys.executable, str(SCRIPT), "ingest", "交付电影", delivery_url, "--type", "movie", "--year", "2026", "--no-transcode", "--no-archive", "--offline"]
            delivery_plan = json.loads(run([*delivery_command, "--dry-run"], env=delivery_env).stdout)
            assert delivery_plan["version"] == "0.4.0" and delivery_plan["configSchemaVersion"] == 1
            delivery_output = delivery_root / "交付电影 (2026)"
            assert Path(delivery_plan["targetPath"]) == delivery_output.resolve()
            assert delivery_plan["target"] == "download"
            run(delivery_command, env=delivery_env)
            assert (delivery_output / "交付电影 (2026).mp4").is_file()
            assert (delivery_output / "movie.nfo").is_file()
            delivery_work = root / "work" / ".media-downloader-work" / delivery_plan["taskId"]
            assert not delivery_work.exists()
            delivery_state = json.loads((root / "status.json").read_text(encoding="utf-8"))[delivery_plan["taskId"]]
            assert delivery_state["targetPath"] == str(delivery_output.resolve())
            assert all(Path(path).is_relative_to(delivery_output.resolve()) for path in delivery_state["archivedFiles"])

            no_deliver_command = [
                sys.executable, str(SCRIPT), "adopt", "仅留工作区", str(root / "source" / "Film.mkv"),
                "--type", "movie", "--no-transcode", "--no-deliver", "--offline",
            ]
            no_deliver_plan = json.loads(run([*no_deliver_command, "--dry-run"], env=delivery_env).stdout)
            assert no_deliver_plan["archive"] is False and no_deliver_plan["target"] == "work"
            run(no_deliver_command, env=delivery_env)
            no_deliver_work = root / "work" / ".media-downloader-work" / no_deliver_plan["taskId"]
            assert (no_deliver_work / "output" / "仅留工作区" / "仅留工作区.mkv").is_file()
            assert not (delivery_root / "仅留工作区").exists()

            # 播放列表必须由单条 ingest 自动连续完成：获取 → 整理 → NFO → 交付 → 清缓存。
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(exist_ok=True)
            fake_ytdlp = fake_bin / "yt-dlp"
            fake_ytdlp.write_text("""#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
if "--version" in sys.argv:
    print("2026.07.04")
    raise SystemExit(0)
args = sys.argv[1:]
output = Path(args[args.index("--paths") + 1])
output.mkdir(parents=True, exist_ok=True)
for index, title in enumerate(("开端", "相逢", "归途"), 1):
    shutil.copy2(os.environ["FAKE_YTDLP_MEDIA"], output / f"{index:03d} {title} [id{index}].mp4")
""", encoding="utf-8")
            fake_ytdlp.chmod(0o755)
            playlist_config = dict(delivery_config)
            playlist_config["namingPresets"] = json.loads(json.dumps(delivery_config["namingPresets"]))
            playlist_config["namingPresets"]["plex"]["tv"]["episodeFile"] = "{canonical} - S{season:02d}E{episode:02d}{episodeTitleSuffix}.{ext}"
            playlist_cfg = root / "playlist-config.json"
            playlist_cfg.write_text(json.dumps(playlist_config, ensure_ascii=False), encoding="utf-8")
            playlist_metadata = root / "playlist-metadata.json"
            playlist_metadata.write_text(json.dumps({
                "title": "列表剧", "year": 2026,
                "episodes": [
                    {"season": 1, "episode": 1, "title": "开端"},
                    {"season": 1, "episode": 2, "title": "相逢"},
                    {"season": 1, "episode": 3, "title": "归途"},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            playlist_env = {
                **delivery_env,
                "MEDIA_DOWNLOADER_CONFIG": str(playlist_cfg),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "FAKE_YTDLP_MEDIA": str(root / "http" / "Remote.S01E01.mp4"),
            }
            playlist_command = [
                sys.executable, str(SCRIPT), "ingest", "列表剧", "https://example.test/playlist",
                "--type", "tv", "--downloader", "yt-dlp", "--playlist", "--season", "1", "--episode", "1",
                "--metadata", str(playlist_metadata), "--no-transcode", "--no-archive", "--offline",
            ]
            playlist_plan = json.loads(run([*playlist_command, "--dry-run"], env=playlist_env).stdout)
            run(playlist_command, env=playlist_env)
            playlist_output = delivery_root / "列表剧 (2026)" / "Season 01"
            for episode_number, title in enumerate(("开端", "相逢", "归途"), 1):
                media = playlist_output / f"列表剧 (2026) - S01E{episode_number:02d} - {title}.mp4"
                assert media.is_file() and media.with_suffix(".nfo").is_file()
                assert_xml(media.with_suffix(".nfo"), f"<title>{title}</title>")
            assert (delivery_root / "列表剧 (2026)" / "tvshow.nfo").is_file()
            assert not (root / "work" / ".media-downloader-work" / playlist_plan["taskId"]).exists()
            playlist_state = json.loads((root / "status.json").read_text(encoding="utf-8"))[playlist_plan["taskId"]]
            assert playlist_state["phase"] == "done" and len(playlist_state["archivedFiles"]) == 7

            # --keep-work 保留缓存和处理产物，同时仍交付下载目录
            keep_command = [sys.executable, str(SCRIPT), "adopt", "保留工作区", str(root / "source" / "Film.mkv"), "--type", "movie", "--no-transcode", "--no-archive", "--offline", "--keep-work"]
            keep_plan = json.loads(run([*keep_command, "--dry-run"], env=delivery_env).stdout)
            run(keep_command, env=delivery_env)
            keep_work = root / "work" / ".media-downloader-work" / keep_plan["taskId"]
            assert keep_work.is_dir()
            assert (keep_work / "output" / "保留工作区" / "保留工作区.mkv").is_file()
            assert (delivery_root / "保留工作区" / "保留工作区.mkv").is_file()

            # 成品目录不得放进任务工作区，避免后续清理误删交付物
            unsafe_delivery = dict(delivery_config)
            unsafe_delivery["downloadDir"] = str(root / "work" / ".media-downloader-work" / "Incoming")
            unsafe_delivery_cfg = root / "unsafe-delivery.json"
            unsafe_delivery_cfg.write_text(json.dumps(unsafe_delivery), encoding="utf-8")
            unsafe_delivery_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(unsafe_delivery_cfg)}
            unsafe_result = run([*delivery_command, "--dry-run"], env=unsafe_delivery_env, expect=1)
            assert "不得位于任务工作区内" in unsafe_result.stderr

            # 无 TMDB key 且 metadata 无海报时 requireArtwork 自动降级，任务可完成
            artwork = json.loads(cfg.read_text(encoding="utf-8"))
            artwork["metadata"] = {"provider": "tmdb", "apiKeyEnv": "UNSET_TEST_TMDB_KEY", "tvFallback": "none", "requireArtwork": True}
            artwork_cfg = root / "artwork.json"
            artwork_cfg.write_text(json.dumps(artwork), encoding="utf-8")
            artwork_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(artwork_cfg)}
            artwork_env.pop("UNSET_TEST_TMDB_KEY", None)
            downgraded = run([sys.executable, str(SCRIPT), "adopt", "降级剧", str(root / "source" / "Example.S02E03.mkv"), "--type", "tv", "--target", "tv", "--offline"], env=artwork_env)
            assert "降级" in downgraded.stderr
            assert (root / "tv" / "降级剧" / "Season 02" / "降级剧 - S02E03.mp4").is_file()
            offline_key_env = {**artwork_env, "UNSET_TEST_TMDB_KEY": "present"}
            offline_key = run([sys.executable, str(SCRIPT), "adopt", "离线有 Key", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--offline"], env=offline_key_env)
            assert "离线模式" in offline_key.stderr
            assert (root / "movie" / "离线有 Key" / "离线有 Key.mp4").is_file()

            relative_env = {**no_archive_env, "MEDIA_DOWNLOADER_STATUS_FILE": str(root / "relative-status.json")}
            relative = run([str(PROJECT / "run.sh"), "adopt", "Relative Background", "Film.mkv", "--type", "movie", "--no-transcode", "--no-archive", "--offline"], env=relative_env, cwd=root / "source")
            launch_log = Path(next(line.split(": ", 1)[1] for line in relative.stdout.splitlines() if line.startswith("Launch log: ")))
            try:
                deadline = time.time() + 20
                while time.time() < deadline:
                    states = json.loads((root / "relative-status.json").read_text(encoding="utf-8")) if (root / "relative-status.json").exists() else {}
                    state = next((item for item in states.values() if item.get("requestedTitle") == "Relative Background"), {})
                    if state.get("phase") in {"done", "failed", "stopped"}:
                        break
                    time.sleep(0.1)
                assert state.get("phase") == "done", (state, launch_log.read_text(encoding="utf-8") if launch_log.exists() else "")
                assert Path(state["targetPath"]).joinpath("Relative Background.mkv").is_file()
            finally:
                launch_log.unlink(missing_ok=True)

            # stop 必须等待父任务和 aria2 进程组退出，并把最终状态写成 stopped。
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(exist_ok=True)
            fake_aria2 = fake_bin / "aria2c"
            fake_aria2.write_text("#!/bin/sh\ntrap 'exit 0' TERM INT\nwhile :; do sleep 1; done\n", encoding="utf-8")
            fake_aria2.chmod(0o700)
            stop_env = {**env, "PATH": f"{fake_bin}:{env.get('PATH', '')}"}
            stop_title = "停止测试"
            stop_process = subprocess.Popen([
                sys.executable, str(SCRIPT), "ingest", stop_title,
                "magnet:?xt=urn:btih:ABCDEF0123456789&dn=stop-test",
                "--type", "movie", "--target", "movie", "--offline", "--no-transcode",
            ], cwd=PROJECT, env=stop_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stop_child_pid = None
            try:
                deadline = time.time() + 10
                while time.time() < deadline:
                    states = json.loads((root / "status.json").read_text(encoding="utf-8")) if (root / "status.json").exists() else {}
                    stop_state = next((item for item in states.values() if item.get("requestedTitle") == stop_title), {})
                    if stop_state.get("phase") == "downloading" and isinstance(stop_state.get("childPid"), int):
                        stop_child_pid = stop_state["childPid"]
                        break
                    time.sleep(0.1)
                assert stop_child_pid, stop_state
                stopped = run([sys.executable, str(SCRIPT), "stop", stop_title], env=stop_env)
                assert "已发送停止信号" in stopped.stdout
                stop_process.wait(timeout=15)
                states = json.loads((root / "status.json").read_text(encoding="utf-8"))
                stop_state = next(item for item in states.values() if item.get("requestedTitle") == stop_title)
                assert stop_state["phase"] == "stopped", stop_state
                assert subprocess.run(["ps", "-p", str(stop_child_pid)], capture_output=True).returncode != 0
            finally:
                if stop_process.poll() is None:
                    stop_process.kill()
                    stop_process.wait(timeout=5)
                if stop_child_pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(stop_child_pid, signal.SIGKILL)

            searched = run([sys.executable, str(SCRIPT), "search", "Remote", "--source", "jackett", "--type", "tv"], env=env)
            payload = json.loads(searched.stdout)
            assert len(payload["candidates"]) == 1
            assert "downloadUrl" not in payload["candidates"][0]
            assert Handler.last_query["apikey"] == ["secret-key"]
            searched_override = run([sys.executable, str(SCRIPT), "search", "Remote", "--source", "jackett", "--type", "tv", "--timeout", "90"], env=env)
            assert len(json.loads(searched_override.stdout)["candidates"]) == 1
            invalid_search_timeout = run([sys.executable, str(SCRIPT), "search", "Remote", "--source", "jackett", "--timeout", "301"], env=env, expect=1)
            assert "1-300" in invalid_search_timeout.stderr
            candidate = payload["candidates"][0]["candidateId"]
            generic = run([sys.executable, str(SCRIPT), "search", "Remote", "--source", "prowlarr", "--type", "tv"], env=env)
            assert len(json.loads(generic.stdout)["candidates"]) == 1
            assert Handler.last_query["apikey"] == ["secret-key"]

            run([sys.executable, str(SCRIPT), "ingest", "Remote", "--candidate", candidate, "--type", "tv", "--target", "tv", "--offline"], env=env)
            remote = root / "tv" / "Remote" / "Season 01" / "Remote - S01E01.mp4"
            assert remote.is_file()

            web_url = f"http://127.0.0.1:{port}/Remote.S01E01.mp4"
            probed = run([sys.executable, str(SCRIPT), "probe", web_url], env=env)
            probe_payload = json.loads(probed.stdout)
            assert probe_payload["kind"] == "video"
            assert probe_payload["source"].startswith("http://127.0.0.1:")
            assert probe_payload["formats"], probe_payload
            invalid_movie_playlist = run([
                sys.executable, str(SCRIPT), "ingest", "Web Playlist", web_url,
                "--downloader", "yt-dlp", "--type", "movie", "--target", "movie", "--playlist", "--dry-run", "--offline",
            ], env=env, expect=1)
            assert "--playlist 必须使用 --type tv" in invalid_movie_playlist.stderr
            run([sys.executable, str(SCRIPT), "ingest", "Web Remote", web_url, "--downloader", "yt-dlp", "--type", "tv", "--target", "tv", "--offline"], env=env)
            web_remote = root / "tv" / "Web Remote" / "Season 01" / "Web Remote - S01E01.mp4"
            assert web_remote.is_file()

            run([sys.executable, str(SCRIPT), "adopt", "示例剧", str(root / "source" / "Example.S02E03.mkv"), "--type", "tv", "--year", "2026", "--target", "tv", "--metadata", str(metadata), "--offline"], env=env)
            show = root / "tv" / "示例剧 (2026)"
            episode = show / "Season 02" / "示例剧 (2026) - S02E03.mp4"
            assert episode.is_file()
            assert episode.with_name(f"{episode.stem}.zh.forced.srt").is_file()
            assert (show / "poster.jpg").is_file()
            assert_xml(show / "tvshow.nfo", "<uniqueid type=\"tmdb\" default=\"true\">123</uniqueid>")
            assert_xml(episode.with_suffix(".nfo"), "<title>第三集</title>")
            assert_xml(episode.with_suffix(".nfo"), "<uniqueid type=\"tmdb\">999</uniqueid>")
            episode_digest = episode.read_bytes()
            poster_digest = (show / "poster.jpg").read_bytes()

            titled_metadata = root / "titled-metadata.json"
            titled_metadata.write_text(json.dumps({
                "title": "标题剧", "year": 2026,
                "episodes": [{
                    "season": 2, "episode": 3, "title": "可靠的单集标题", "ids": {"tmdb": 1003},
                    "thumbPath": str(root / "source" / "poster.png"),
                }],
            }, ensure_ascii=False), encoding="utf-8")
            titled_command = [
                sys.executable, str(SCRIPT), "organize", "标题剧", str(root / "source" / "Example.S02E03.mkv"),
                "--type", "tv", "--year", "2026", "--target", "tv", "--naming", "plex-title",
                "--metadata", str(titled_metadata), "--offline",
            ]
            run(titled_command, env=env)
            titled_episode = root / "tv" / "标题剧 (2026)" / "Season 02" / "标题剧 (2026) - S02E03 - 可靠的单集标题.mkv"
            assert titled_episode.is_file()
            assert_xml(titled_episode.with_suffix(".nfo"), "<title>可靠的单集标题</title>")
            assert_xml(titled_episode.with_suffix(".nfo"), "<uniqueid type=\"tmdb\">1003</uniqueid>")
            assert titled_episode.with_name(f"{titled_episode.stem}-thumb.jpg").is_file()

            # 存量库修复默认只预览；仅可靠标题改名，Season 0/字幕/NFO 一起处理，未知标题保留原名。
            repair_library = root / "repair-library"
            repair_show = repair_library / "存量剧 (2026)"
            repair_season = repair_show / "Season 00"
            repair_season.mkdir(parents=True)
            repair_tvshow = repair_show / "tvshow.nfo"
            repair_tvshow.write_bytes(b"existing show nfo")
            repair_old = repair_season / "Legacy.S00E01.mkv"
            repair_untitled = repair_season / "Legacy.S00E02.mkv"
            shutil.copy2(root / "source" / "Example.S02E03.mkv", repair_old)
            shutil.copy2(root / "source" / "Example.S02E03.mkv", repair_untitled)
            repair_old.with_name(f"{repair_old.stem}.zh.srt").write_text("subtitle", encoding="utf-8")
            repair_old.with_suffix(".nfo").write_text("old episode nfo", encoding="utf-8")
            repair_metadata = root / "repair-metadata.json"
            repair_metadata.write_text(json.dumps({
                "title": "存量剧", "year": 2026,
                "episodes": [{"season": 0, "episode": 1, "title": "特别篇", "plot": "特别剧情", "ids": {"tmdb": 7001}}],
            }, ensure_ascii=False), encoding="utf-8")
            repair_command = [
                sys.executable, str(SCRIPT), "repair", "存量剧", str(repair_show), "--year", "2026", "--season", "0",
                "--naming", "plex-title", "--metadata", str(repair_metadata), "--offline", "--update-nfo",
            ]
            repair_preview = json.loads(run(repair_command, env=env).stdout)
            assert repair_preview["mode"] == "preview" and repair_preview["renameCount"] == 3
            assert repair_old.is_file() and repair_untitled.is_file(), "preview must not modify the library"
            broad_repair = run([
                sys.executable, str(SCRIPT), "repair", "存量剧", str(repair_library), "--year", "2026", "--season", "0",
                "--metadata", str(repair_metadata), "--offline",
            ], env=env, expect=1)
            assert "单部剧目录" in broad_repair.stderr
            repair_applied = json.loads(run([*repair_command, "--apply"], env=env).stdout)
            assert repair_applied["mode"] == "apply"
            repaired = repair_season / "存量剧 (2026) - S00E01 - 特别篇.mkv"
            assert repaired.is_file() and not repair_old.exists()
            assert repaired.with_name(f"{repaired.stem}.zh.srt").read_text(encoding="utf-8") == "subtitle"
            assert_xml(repaired.with_suffix(".nfo"), "<title>特别篇</title>")
            assert_xml(repaired.with_suffix(".nfo"), "<uniqueid type=\"tmdb\">7001</uniqueid>")
            assert repair_untitled.is_file(), "episodes without reliable titles must keep their original paths"
            assert repair_tvshow.read_bytes() == b"existing show nfo"
            repair_repeat = json.loads(run(repair_command, env=env).stdout)
            assert repair_repeat["renameCount"] == 0 and repair_repeat["nfoUpdateCount"] == 1

            titled_metadata.write_text(json.dumps({
                "title": "标题剧", "year": 2026,
                "episodes": [{"season": 2, "episode": 3, "title": "后来修改的标题", "ids": {"tmdb": 1003}}],
            }, ensure_ascii=False), encoding="utf-8")
            duplicate_title = run(titled_command, env=env, expect=1)
            assert "同一季集已存在" in duplicate_title.stderr
            assert len(list(titled_episode.parent.glob("*.mkv"))) == 1

            # 分集并行归档：冲突先预检，普通模式不得产生部分视频；--merge 只跳过节目级共享文件。
            increment_source = root / "source" / "Increment.S02E04.mkv"
            make_video(increment_source)
            increment_metadata = root / "increment-metadata.json"
            increment_metadata.write_text(json.dumps({
                "title": "增量剧", "year": 2026, "fanartPath": str(root / "source" / "poster.png"),
                "episodes": [{"season": 2, "episode": 4, "title": "第四集"}],
            }, ensure_ascii=False), encoding="utf-8")
            increment_show = root / "tv" / "增量剧 (2026)"
            increment_show.mkdir()
            (increment_show / "fanart.jpg").write_bytes(b"existing fanart")
            (increment_show / "tvshow.nfo").write_bytes(b"existing nfo")
            increment_command = [sys.executable, str(SCRIPT), "adopt", "增量剧", str(increment_source), "--type", "tv", "--year", "2026", "--target", "tv", "--metadata", str(increment_metadata), "--offline"]
            refused_merge = run(increment_command, env=env, expect=1)
            assert "拒绝覆盖" in refused_merge.stderr
            increment_episode = increment_show / "Season 02" / "增量剧 (2026) - S02E04.mp4"
            assert not increment_episode.exists(), "archive preflight must prevent partial media delivery"
            merged = run([*increment_command, "--merge"], env=env)
            assert "合并跳过已有共享文件" in merged.stdout
            assert increment_episode.is_file()
            assert increment_episode.with_suffix(".nfo").is_file()
            assert (increment_show / "fanart.jpg").read_bytes() == b"existing fanart"
            assert (increment_show / "tvshow.nfo").read_bytes() == b"existing nfo"

            metadata.write_text(json.dumps({
                "title": "示例剧", "originalTitle": "Example Show", "year": 2026,
                "plot": "更新后的剧集简介", "ids": {"tmdb": 123}, "posterPath": str(root / "source" / "poster.png"),
                "episodes": [{"season": 2, "episode": 3, "title": "修正后的第三集", "plot": "更新后的单集简介", "ids": {"tmdb": 999}}],
            }, ensure_ascii=False), encoding="utf-8")
            refused_nfo = run([sys.executable, str(SCRIPT), "adopt", "示例剧", str(root / "source" / "Example.S02E03.mkv"), "--type", "tv", "--year", "2026", "--target", "tv", "--metadata", str(metadata), "--offline"], env=env, expect=1)
            assert "拒绝覆盖" in refused_nfo.stderr
            run([sys.executable, str(SCRIPT), "adopt", "示例剧", str(root / "source" / "Example.S02E03.mkv"), "--type", "tv", "--year", "2026", "--target", "tv", "--metadata", str(metadata), "--offline", "--update-nfo"], env=env)
            assert_xml(show / "tvshow.nfo", "<plot>更新后的剧集简介</plot>")
            assert_xml(episode.with_suffix(".nfo"), "<title>修正后的第三集</title>")
            assert episode.read_bytes() == episode_digest
            assert (show / "poster.jpg").read_bytes() == poster_digest

            organize_tv_source = root / "source" / "Organize.Show.S04E05.mkv"
            run([sys.executable, str(SCRIPT), "organize", "原样剧集", str(organize_tv_source), "--type", "tv", "--target", "tv", "--offline"], env=env)
            organized_episode = root / "tv" / "原样剧集" / "Season 04" / "原样剧集 - S04E05.mkv"
            assert organized_episode.read_bytes() == organize_tv_source.read_bytes()
            assert organized_episode.with_suffix(".nfo").is_file()
            assert not organized_episode.with_suffix(".mp4").exists()

            organize_movie_source = root / "source" / "Organize.Movie.mkv"
            run([sys.executable, str(SCRIPT), "organize", "原样电影", str(organize_movie_source), "--type", "movie", "--target", "movie", "--offline"], env=env)
            organized_movie = root / "movie" / "原样电影" / "原样电影.mkv"
            assert organized_movie.read_bytes() == organize_movie_source.read_bytes()
            assert (organized_movie.parent / "movie.nfo").is_file()
            assert not organized_movie.with_suffix(".mp4").exists()

            # MKV 转码保留原有内嵌字幕流；MP4 转码仍按兼容性丢弃内嵌字幕（外挂字幕不受影响）。
            run([sys.executable, str(SCRIPT), "adopt", "带字幕电影", str(root / "source" / "Subtitled.Movie.mkv"), "--type", "movie", "--profile", "movie-mkv", "--target", "movie", "--offline"], env=env)
            mkv_movie = root / "movie" / "带字幕电影" / "带字幕电影.mkv"
            assert mkv_movie.is_file()
            subtitle_probe = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(mkv_movie)])
            assert "subtitle" in subtitle_probe.stdout, subtitle_probe.stdout
            local_subs = run([sys.executable, str(SCRIPT), "adopt", "字幕本地", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--write-subs", "--offline"], env=env, expect=1)
            assert "--write-subs 只适用于" in local_subs.stderr

            source_conflict = root / "source" / "Conflict.S01E01.mkv"
            make_video(source_conflict)
            conflict_target = root / "tv" / "Conflict" / "Season 01" / "Conflict - S01E01.mp4"
            conflict_target.parent.mkdir(parents=True)
            conflict_target.write_bytes(b"different")
            conflict = run([sys.executable, str(SCRIPT), "adopt", "Conflict", str(source_conflict), "--type", "tv", "--target", "tv", "--offline", "--merge"], env=env, expect=1)
            assert "拒绝覆盖" in conflict.stderr
            assert source_conflict.is_file()
            failed_states = json.loads((root / "status.json").read_text(encoding="utf-8"))
            failed_state = next(item for item in failed_states.values() if item.get("title") == "Conflict")
            assert failed_state["phase"] == "failed"
            task_work = root / "work" / ".media-downloader-work" / next(key for key, item in failed_states.items() if item.get("title") == "Conflict")
            assert task_work.is_dir()
            assert stat.S_IMODE(task_work.stat().st_mode) == 0o700
            conflict_id = task_work.name
            lock_path = root / "state" / f"{conflict_id}.lock"
            with open(lock_path, "a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                locked_reset = run([sys.executable, str(SCRIPT), "adopt", "Conflict", str(source_conflict), "--type", "tv", "--target", "tv", "--offline", "--reset-work"], env=env, expect=1)
                assert "任务已在运行" in locked_reset.stderr
            assert task_work.is_dir()

            run([sys.executable, str(SCRIPT), "adopt", "待核实电影", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--metadata", str(movie_metadata), "--offline"], env=env)
            film = root / "movie" / "测试电影 (2025)" / "测试电影 (2025).mp4"
            assert film.is_file()
            assert_xml(film.parent / "movie.nfo", "<movie>")
            assert_xml(film.parent / "movie.nfo", "<sorttitle>Test Film, The</sorttitle>")
            assert_xml(film.parent / "movie.nfo", '<rating name="themoviedb" max="10" default="true">')
            assert_xml(film.parent / "movie.nfo", "<value>7.5</value>")
            assert_xml(film.parent / "movie.nfo", "<votes>1550</votes>")
            assert_xml(film.parent / "movie.nfo", "<name>测试演员</name>")
            assert (film.parent / "banner.jpg").is_file()
            assert (film.parent / "clearlogo.png").is_file()
            movie_state = next(item for item in json.loads((root / "status.json").read_text(encoding="utf-8")).values() if item.get("requestedTitle") == "待核实电影")
            assert movie_state["title"] == "测试电影 (2025)"
            task_log = Path(movie_state["logPath"])
            assert task_log.resolve().is_relative_to((root / "state").resolve())
            assert stat.S_IMODE(task_log.stat().st_mode) == 0o600

            # movie 路径 --update-nfo：只更新 movie.nfo，媒体与海报字节不变
            movie_nfo = film.parent / "movie.nfo"
            film_digest = film.read_bytes()
            movie_poster_digest = (film.parent / "poster.jpg").read_bytes()
            movie_metadata.write_text(json.dumps({
                "title": "测试电影", "originalTitle": "Test Film", "year": 2025,
                "plot": "更新后的电影剧情", "posterPath": str(root / "source" / "poster.png"),
            }, ensure_ascii=False), encoding="utf-8")
            refused_movie = run([sys.executable, str(SCRIPT), "adopt", "待核实电影", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--metadata", str(movie_metadata), "--offline"], env=env, expect=1)
            assert "拒绝覆盖" in refused_movie.stderr
            run([sys.executable, str(SCRIPT), "adopt", "待核实电影", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--metadata", str(movie_metadata), "--offline", "--update-nfo"], env=env)
            assert_xml(movie_nfo, "<plot>更新后的电影剧情</plot>")
            assert film.read_bytes() == film_digest
            assert (film.parent / "poster.jpg").read_bytes() == movie_poster_digest

            no_token = root / "source" / "NoToken.mkv"
            no_token.write_bytes((root / "source" / "Film.mkv").read_bytes())
            missing_episode = run([sys.executable, str(SCRIPT), "adopt", "缺少集号", str(no_token), "--type", "tv", "--target", "tv", "--offline"], env=env, expect=1)
            assert "显式传 --season/--episode" in missing_episode.stderr
            run([sys.executable, str(SCRIPT), "adopt", "缺少集号", str(no_token), "--type", "tv", "--target", "tv", "--episode", "7", "--offline", "--reset-work"], env=env)
            assert (root / "tv" / "缺少集号" / "Season 01" / "缺少集号 - S01E07.mp4").is_file()

            multi = root / "source" / "Multi.S01E01-E02.mkv"
            multi.write_bytes((root / "source" / "Film.mkv").read_bytes())
            multi_result = run([sys.executable, str(SCRIPT), "adopt", "多集", str(multi), "--type", "tv", "--target", "tv", "--offline"], env=env, expect=1)
            assert "多集单文件" in multi_result.stderr

            overlap = json.loads(cfg.read_text(encoding="utf-8"))
            overlap["baseDir"] = str(root / "tv")
            overlap_cfg = root / "overlap.json"
            overlap_cfg.write_text(json.dumps(overlap), encoding="utf-8")
            bad_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(overlap_cfg)}
            failed = run([sys.executable, str(SCRIPT), "adopt", "Unsafe", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "tv", "--offline"], env=bad_env, expect=1)
            assert "不得重叠" in failed.stderr

            injection = run([sys.executable, str(SCRIPT), "ingest", "Unsafe", "https://example.test/a\nhttps://evil.test/b", "--type", "movie", "--target", "movie", "--offline"], env=env, expect=1)
            assert "控制字符" in injection.stderr

            private_source = root / "private-source"
            private_source.write_text(str(root / "source" / "Film.mkv") + "\n", encoding="utf-8")
            private_source.chmod(0o600)
            dry_run = run([sys.executable, str(SCRIPT), "ingest", "Private", "--source-file", str(private_source), "--type", "movie", "--target", "movie", "--offline", "--dry-run"], env=env)
            assert json.loads(dry_run.stdout)["source"].endswith("Film.mkv")
            private_source.chmod(0o644)
            insecure = run([sys.executable, str(SCRIPT), "ingest", "Private", "--source-file", str(private_source), "--type", "movie", "--target", "movie", "--offline", "--dry-run"], env=env, expect=1)
            assert "组/其他权限" in insecure.stderr
            source_link = root / "private-source-link"
            source_link.symlink_to(private_source)
            linked = run([sys.executable, str(SCRIPT), "ingest", "Private", "--source-file", str(source_link), "--type", "movie", "--target", "movie", "--offline", "--dry-run"], env=env, expect=1)
            assert "安全读取" in linked.stderr

            bad_metadata = root / "bad-metadata.json"
            bad_metadata.write_text(json.dumps({"title": "Bad", "genres": "Drama"}), encoding="utf-8")
            invalid_metadata = run([sys.executable, str(SCRIPT), "adopt", "Bad", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--metadata", str(bad_metadata), "--offline", "--dry-run"], env=env, expect=1)
            assert "metadata.genres" in invalid_metadata.stderr

            statuses = json.loads((root / "status.json").read_text(encoding="utf-8"))
            assert {item["phase"] for item in statuses.values()} == {"done", "failed", "stopped"}

    print("integration test passed")


if __name__ == "__main__":
    import sys
    main()
