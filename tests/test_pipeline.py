#!/usr/bin/env python3
"""One dependency-free integration check for the full media ingest contract."""

from __future__ import annotations

import contextlib
import fcntl
import http.server
import importlib.util
import io
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
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "media-downloader.py"


def run(command, *, env=None, expect=0, cwd=PROJECT):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != expect:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expect}: {command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def write_config(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


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
    attachment = path.with_name(f".{path.stem}.font.txt")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\n保留字幕\n", encoding="utf-8")
    attachment.write_text("subtitle font attachment", encoding="utf-8")
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=1000",
        "-i", str(subtitle),
        "-t", str(seconds), "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-metadata", "title=源容器标题",
        "-metadata:s:v:0", "title=视频轨道", "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=日语音轨",
        "-metadata:s:s:0", "language=chi", "-metadata:s:s:0", "title=中文",
        "-attach", str(attachment), "-metadata:s:t:0", "mimetype=text/plain", "-metadata:s:t:0", "filename=font.txt",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt",
        str(path),
    ])
    subtitle.unlink()
    attachment.unlink()


def make_image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=80x120", "-frames:v", "1", str(path)])


class Handler(http.server.SimpleHTTPRequestHandler):
    root: Path
    last_query = {}
    retry_counts = {}
    retry_mode = "success"

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.endswith("/torznab/api"):
            type(self).last_query = urllib.parse.parse_qs(parsed.query)
            if type(self).last_query.get("t") == ["caps"]:
                if type(self).retry_mode == "caps-fail":
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = b"<?xml version='1.0'?><caps><server version='1'/></caps>"
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
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
        if parsed.path == "/retry.mp4":
            count = type(self).retry_counts.get(parsed.path, 0) + 1
            type(self).retry_counts[parsed.path] = count
            if type(self).retry_mode == "retry-once" and count == 1 or type(self).retry_mode == "always-fail":
                self.send_response(503)
                self.end_headers()
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
    return write_config(root / "config.json", data)


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

    # 日志脱敏必须重建为 0600，且不留默认权限的临时副本。
    log_path = root / "scrub-log.txt"
    log_path.write_text("failed https://example.test/private/video?token=DO_NOT_LEAK\n", encoding="utf-8")
    log_path.chmod(0o644)
    module.scrub_log(log_path, ["https://example.test/private/video?token=DO_NOT_LEAK"])
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600, oct(log_path.stat().st_mode)
    assert "DO_NOT_LEAK" not in log_path.read_text(encoding="utf-8")
    assert not list(root.glob(".scrub-log.txt*")), "scrub temp files must be removed"
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
            return {"poster_path": "/stub-season.jpg", "episodes": [{
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
        assert fetched["seasonPosterUrl"].endswith("/stub-season.jpg")
        assert fetched["seasonNumber"] == 2
        calls.clear()
        # v3 key 保持 api_key 参数，不带 Authorization header
        os.environ["TEST_TMDB_AUTH_KEY"] = "v3-api-key-123"
        module.fetch_tmdb(config, "tv", "Stub", None, 2)
        assert calls[0][1].get("api_key") == "v3-api-key-123", calls
        assert not calls[0][2].get("Authorization"), calls
        assert calls[1][1].get("api_key") == "v3-api-key-123", calls

        # 同名条目必须优先于热度更高的其他条目；没有同名时才回退到热度。
        def ranked(url, params, headers=None, timeout=20):
            calls.append((url, dict(params), dict(headers or {})))
            if "/search/" in url:
                return {"results": [
                    {"id": 1, "name": "Stub Special", "popularity": 900.0},
                    {"id": 2, "name": "Stub", "popularity": 1.0},
                ]}
            return {"id": int(url.rsplit("/", 1)[-1]), "name": "Stub", "first_air_date": "2020-01-01"}

        module.http_json = ranked
        calls.clear()
        assert module.fetch_tmdb(config, "tv", "Stub", None, None)["ids"]["tmdb"] == 2
        assert calls[1][0].endswith("/tv/2"), calls
        calls.clear()
        assert module.fetch_tmdb(config, "tv", "Unrelated", None, None)["ids"]["tmdb"] == 1
        assert calls[1][0].endswith("/tv/1"), calls
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

    ctx_a = {"mediaType": "tv", "canonical": "测试剧 (2026)", "targetRoot": root / "tv"}
    id_magnet_a = module.pipeline_task_id(ctx_a, "magnet:?xt=urn:btih:AAAA")
    id_magnet_b = module.pipeline_task_id(ctx_a, "magnet:?xt=urn:btih:BBBB")
    assert id_magnet_a != id_magnet_b
    assert id_magnet_a == module.pipeline_task_id(ctx_a, "magnet:?xt=urn:btih:AAAA")
    assert id_magnet_a != module.pipeline_task_id({**ctx_a, "canonical": "另一部剧 (2026)"}, "magnet:?xt=urn:btih:AAAA")
    assert id_magnet_a != module.pipeline_task_id({**ctx_a, "targetRoot": root / "other-tv"}, "magnet:?xt=urn:btih:AAAA")
    fingerprint_args = type("Args", (), {
        "downloader": "yt-dlp", "copy_original": True, "season": 1, "episode": 1,
        "playlist": False, "format": "best", "cookies": "chrome",
        "write_subs": True, "sub_langs": "zh-CN",
    })()
    fingerprint_ctx = {
        "canonical": "Show", "mediaType": "tv", "profile": {"container": "mkv"},
        "naming": {"episodeFile": "x"}, "metadata": {}, "config": {"metadata": {}}, "args": fingerprint_args,
    }
    first_fingerprint = module.source_fingerprint(fingerprint_ctx, "https://example.test/video")
    fingerprint_args.sub_langs = "en"
    assert first_fingerprint != module.source_fingerprint(fingerprint_ctx, "https://example.test/video")
    local_file = root / "fingerprint-source.mkv"
    local_file.write_bytes(b"source-v1")
    local_first = module.source_fingerprint(fingerprint_ctx, str(local_file))
    local_file.write_bytes(b"source-v2-with-a-different-size")
    assert local_first != module.source_fingerprint(fingerprint_ctx, str(local_file))
    # A single local video fingerprints the sidecars consumed during output.
    single_video = root / "single.mkv"
    single_video.write_bytes(b"video")
    single_first = module.local_source_snapshot(str(single_video))
    subtitle = root / "single.zh.srt"
    subtitle.write_text("subtitle", encoding="utf-8")
    single_with_subtitle = module.local_source_snapshot(str(single_video))
    assert single_first != single_with_subtitle
    subtitle.write_text("subtitle changed", encoding="utf-8")
    assert single_with_subtitle != module.local_source_snapshot(str(single_video))
    subtitle.unlink()
    assert single_first == module.local_source_snapshot(str(single_video))
    poster = root / "poster.jpg"
    poster.write_bytes(b"poster")
    with_poster = module.local_source_snapshot(str(single_video))
    assert with_poster != single_first
    poster.write_bytes(b"poster changed")
    assert with_poster != module.local_source_snapshot(str(single_video))
    poster.unlink()
    assert single_first == module.local_source_snapshot(str(single_video))
    (root / "other.zh.srt").write_text("unrelated", encoding="utf-8")
    assert single_first == module.local_source_snapshot(str(single_video))
    (root / "irrelevant.txt").write_text("ignore", encoding="utf-8")
    assert single_first == module.local_source_snapshot(str(single_video))
    local_dir = root / "fingerprint-directory"
    local_dir.mkdir()
    (local_dir / "episode.mkv").write_bytes(b"episode")
    directory_first = module.source_fingerprint(fingerprint_ctx, str(local_dir))
    (local_dir / "episode.zh.srt").write_text("subtitle", encoding="utf-8")
    assert directory_first != module.source_fingerprint(fingerprint_ctx, str(local_dir))
    assert module.validate_redirect_target("http://example.test/a", "https://example.test/b").endswith("/b")
    try:
        module.validate_redirect_target("https://example.test/a", "https://other.test/b")
    except RuntimeError as exc:
        assert "跨主机" in str(exc)
    else:
        raise AssertionError("cross-host redirects must be rejected")
    try:
        module.validate_redirect_target("https://example.test/a", "http://example.test/b")
    except RuntimeError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("HTTPS downgrade redirects must be rejected")
    for blocked_url in ("http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/"):
        try:
            module.validate_public_http_url(blocked_url)
        except RuntimeError as exc:
            assert "内网" in str(exc)
        else:
            raise AssertionError(f"private HTTP destination must be rejected: {blocked_url}")
    try:
        module.validate_public_http_url("https://example.test:bad/")
    except RuntimeError as exc:
        assert "URL" in str(exc)
    else:
        raise AssertionError("malformed HTTP ports must be rejected")
    try:
        module.validate_source("https://user:secret@example.test/video", allow_local=False)
    except RuntimeError as exc:
        assert "用户名或密码" in str(exc)
    else:
        raise AssertionError("credential-bearing source URLs must be rejected")
    # FIFO inputs must be rejected without blocking before type validation.
    fifo = root / "private-input.fifo"
    os.mkfifo(fifo, 0o600)
    fifo_probe = subprocess.run([
        sys.executable, "-c",
        "import importlib.util,sys; s=importlib.util.spec_from_file_location('m',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m);\ntry: m.open_private_input(__import__('pathlib').Path(sys.argv[2]), 'fifo', 1024)\nexcept RuntimeError: pass\nelse: raise SystemExit(2)",
        str(SCRIPT), str(fifo),
    ], capture_output=True, text=True, timeout=2)
    assert fifo_probe.returncode == 0, fifo_probe.stderr
    fifo.unlink()
    private_text = root / "private-input.txt"
    private_text.write_text("keep", encoding="utf-8")
    private_text.chmod(0o644)
    try:
        module.write_private_text(private_text, "replace")
    except RuntimeError as exc:
        assert "不安全" in str(exc)
    else:
        raise AssertionError("write_private_text must reject group-readable files")
    assert private_text.read_text(encoding="utf-8") == "keep"
    private_text.chmod(0o600)
    hardlink = root / "private-input-hardlink.txt"
    os.link(private_text, hardlink)
    try:
        module.write_private_text(private_text, "replace")
    except RuntimeError as exc:
        assert "不安全" in str(exc)
    else:
        raise AssertionError("write_private_text must reject hard-linked files")
    assert private_text.read_text(encoding="utf-8") == "keep"
    hardlink.unlink()
    fifo_write = root / "private-write.fifo"
    os.mkfifo(fifo_write, 0o600)
    write_probe = subprocess.run([
        sys.executable, "-c",
        "import importlib.util,sys; s=importlib.util.spec_from_file_location('m',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m);\ntry: m.write_private_text(__import__('pathlib').Path(sys.argv[2]), 'x')\nexcept (RuntimeError,OSError): pass\nelse: raise SystemExit(2)",
        str(SCRIPT), str(fifo_write),
    ], capture_output=True, text=True, timeout=2)
    assert write_probe.returncode == 0, write_probe.stderr
    fifo_write.unlink()
    for lock_path, acquire_lock in (
        (root / ".guard.json.lock", lambda: module.json_lock(root / "guard.json")),
        (root / "guard.lock", lambda: module.task_lock({"stateRoot": root, "id": "guard", "title": "Guard"})),
    ):
        os.link(private_text, lock_path)
        try:
            with acquire_lock():
                raise AssertionError("hard-linked locks must be rejected")
        except RuntimeError as exc:
            assert "不安全" in str(exc)
        finally:
            lock_path.unlink()
        assert private_text.read_text(encoding="utf-8") == "keep"
    try:
        module.metadata_from_file(str(root / "missing-metadata.json"))
    except RuntimeError as exc:
        assert "不存在" in str(exc)
    else:
        raise AssertionError("missing explicit metadata path must fail")
    metadata_dir = root / "metadata-dir"
    metadata_dir.mkdir()
    try:
        module.metadata_from_file(str(metadata_dir))
    except RuntimeError as exc:
        assert "普通文件" in str(exc)
    else:
        raise AssertionError("directory metadata path must fail")
    metadata_calls = []
    original_tmdb, original_tvmaze = module.fetch_tmdb, module.fetch_tvmaze
    offline_env = os.environ.pop("MEDIA_DOWNLOADER_OFFLINE", None)
    module.fetch_tmdb = lambda *_args: metadata_calls.append("tmdb") or {}
    module.fetch_tvmaze = lambda *_args: metadata_calls.append("tvmaze") or {}
    try:
        none_args = type("Args", (), {"metadata": None, "title": "No Lookup", "media_type": "tv", "year": None, "season": 1, "offline": False})()
        resolved = module.resolve_metadata({"metadata": {"provider": "none"}}, none_args)
        assert resolved["title"] == "No Lookup" and metadata_calls == []
        explicit_none_args = type("Args", (), {"metadata": None, "title": "No Fallback", "media_type": "tv", "year": None, "season": 1, "offline": False})()
        module.resolve_metadata({"metadata": {"provider": "none", "tvFallback": "tvmaze"}}, explicit_none_args)
        assert metadata_calls == []
        module.resolve_metadata({}, explicit_none_args)
        assert metadata_calls == []
        tmdb_args = type("Args", (), {"metadata": None, "title": "Fallback", "media_type": "tv", "year": None, "season": 1, "offline": False})()
        module.resolve_metadata({"metadata": {"provider": "tmdb"}}, tmdb_args)
        assert metadata_calls == ["tmdb", "tvmaze"]
        metadata_calls.clear()
        module.resolve_metadata({"metadata": {"tvFallback": "tvmaze"}}, explicit_none_args)
        assert metadata_calls == ["tvmaze"]
    finally:
        module.fetch_tmdb, module.fetch_tvmaze = original_tmdb, original_tvmaze
        if offline_env is not None:
            os.environ["MEDIA_DOWNLOADER_OFFLINE"] = offline_env
    bad_config = {
        "profiles": {"tv": {"type": "tv", "container": "mp4"}, "nut": {"type": "movie", "container": "nut"}},
        "defaultProfiles": {"tv": "tv", "movie": "nut"},
        "namingPresets": {"plex": {"tv": {"showDir": "x", "seasonDir": "x", "episodeFile": "x"}, "movie": {"showDir": "x", "movieFile": "x"}}},
    }
    try:
        module.validate_config(bad_config)
    except RuntimeError as exc:
        assert "容器" in str(exc)
    else:
        raise AssertionError("unsupported output containers must fail config validation")
    sidecar_output = root / "sidecar-only"
    sidecar_output.mkdir()
    (sidecar_output / "Movie.nfo").write_text("<movie/>", encoding="utf-8")
    target_root = root / "sidecar-target"
    target_root.mkdir()
    try:
        module.archive({"outputRoot": sidecar_output, "targetRoot": target_root, "targetIdentity": module.directory_identity(target_root), "targetShow": target_root / "Movie", "mediaType": "movie", "config": {"minMediaDurationSeconds": 0}, "args": type("Args", (), {"merge": False, "update_nfo": False})(), "id": "sidecar-only"})
    except RuntimeError as exc:
        assert "视频文件" in str(exc)
    else:
        raise AssertionError("sidecar-only archive must fail")
    assert list(target_root.iterdir()) == []
    assert (sidecar_output / "Movie.nfo").is_file()
    command = module.ffmpeg_command({"profile": {"container": "mp4", "videoCodec": "libx264", "audioCodec": "aac", "resolution": 720}}, Path("input.mkv"), Path("output.mp4"))
    assert command[command.index("-map_metadata") + 1] == "-1"
    assert command[command.index("-map_chapters") + 1] == "-1"
    assert "-sn" in command and "-c:s" not in command
    mkv_command = module.ffmpeg_command({"profile": {"container": "mkv", "videoCodec": "libx264", "audioCodec": "aac", "resolution": 720}}, Path("input.mkv"), Path("output.mkv"))
    assert "-sn" not in mkv_command
    assert mkv_command[mkv_command.index("0:s?") - 1] == "-map"
    assert mkv_command[mkv_command.index("-c:s") + 1] == "copy"
    mkv_metadata_command = module.ffmpeg_command({"profile": {"container": "mkv", "videoCodec": "libx264", "audioCodec": "aac", "resolution": 720}}, Path("input.mkv"), Path("output.mkv"), {"videoStreams": 1, "audioStreams": 2, "subtitleStreams": 2, "attachmentStreams": 1})
    assert mkv_metadata_command[mkv_metadata_command.index("-map_metadata:s:v:0") + 1] == "0:s:v:0"
    assert mkv_metadata_command[mkv_metadata_command.index("-map_metadata:s:a:1") + 1] == "0:s:a:1"
    assert mkv_metadata_command[mkv_metadata_command.index("-map_metadata:s:s:1") + 1] == "0:s:s:1"
    assert mkv_metadata_command[mkv_metadata_command.index("-map_metadata:s:t:0") + 1] == "0:s:t:0"
    assert mkv_metadata_command[mkv_metadata_command.index("-c:t") + 1] == "copy"
    module.validate_preserved_streams(
        {"audioStreams": 2, "subtitleStreams": 2, "attachmentStreams": 1},
        {"audioStreams": 2, "subtitleStreams": 2, "attachmentStreams": 1},
        "mkv",
    )
    try:
        module.validate_preserved_streams(
            {"audioStreams": 2, "subtitleStreams": 2, "attachmentStreams": 1},
            {"audioStreams": 2, "subtitleStreams": 1, "attachmentStreams": 1},
            "mkv",
        )
    except RuntimeError as exc:
        assert "字幕流" in str(exc)
    else:
        raise AssertionError("missing mapped subtitle streams must fail validation")
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
        assert "--max-tries=4" in captured[0] and "--retry-wait=3" in captured[0], captured
        module.acquire({
            "sourceRoot": magnet_source,
            "workRoot": magnet_work,
            "config": {"timeoutHours": 1, "downloadRetries": 0},
            "args": type("Args", (), {"playlist": False})(),
        }, long_magnet, "auto")
        assert "--max-tries=1" in captured[-1], captured[-1]
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
    try:
        module.validate_subtitle_languages(" , ")
    except ValueError as exc:
        assert "不能为空" in str(exc)
    else:
        raise AssertionError("empty subtitle language selection must be rejected")
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
        assert "--abort-on-unavailable-fragments" in command
        assert command[command.index("--retries") + 1] == "3"
        assert command.count("--retry-sleep") == 3
        assert "--write-subs" in command and "--write-auto-subs" in command
        assert command[command.index("--sub-format") + 1] == "srt/best"
        assert command[command.index("--sub-langs") + 1] == "zh-CN,en"
        assert command[command.index("--convert-subs") + 1] == "srt"
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
    assert module.missing_episode_report(report_ctx, [{"season": 1, "episode": 3}]) is None
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
    make_video(race_output / "Episode.mkv")
    (race_output / "fanart.jpg").write_bytes(b"incoming")
    race_logs = []
    original_atomic, original_log, original_status = module.atomic_copy, module.log, module.status_update
    try:
        def race_atomic(source, target, minimum):
            if target.name == "fanart.jpg":
                target.write_bytes(b"other task")
                raise RuntimeError("simulated concurrent writer")
            return original_atomic(source, target, minimum)

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
        assert archived == [str(race_target_show / "Episode.mkv"), str(race_target_show / "fanart.jpg")]
        assert (race_target_show / "Episode.mkv").is_file()
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


def assert_doctor_contract(module, data: dict):
    data = {**data, "targets": {}, "searchSources": {}, "metadata": {"provider": "tmdb", "apiKeyEnv": "DOCTOR_TEST_KEY"}}
    secret = "doctor-private-test-value"

    def fake_tool(command, **_kwargs):
        if "--verbose" in command:
            return subprocess.CompletedProcess(command, 2, "", f"[debug] Optional libraries: yt_dlp_ejs-0.8.0\n[debug] JS runtimes: deno-2.9.5\n[debug] Proxy map: {secret}\n")
        return subprocess.CompletedProcess(command, 0, "2026.07.04\n", "")

    def doctor(*flags):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.command_doctor(module.parser().parse_args(["doctor", *flags]))
        assert secret not in output.getvalue()
        return code, {item["name"]: item for item in json.loads(output.getvalue())["checks"]}

    with patch.dict(os.environ, {"DOCTOR_TEST_KEY": secret}, clear=True), patch.object(module, "load_config", return_value=data), patch.object(module.shutil, "which", side_effect=lambda tool: f"/mock/{tool}"), patch.object(module.subprocess, "run", side_effect=fake_tool), patch.object(module, "http_open") as http:
        code, checks = doctor("--cookies", "chrome")
        http.assert_not_called()
        assert code == 0 and checks["runtime:deno"]["status"] == checks["yt-dlp:ejs"]["status"] == "ok"
        assert checks["cookies"]["status"] == "unverified"
        with patch.object(module.shutil, "which", side_effect=lambda tool: f"/mock/{tool}" if tool in {"ffmpeg", "ffprobe"} else None):
            code, checks = doctor()
            assert code == 0 and checks["deno"]["status"] == "optional-missing"
        http.return_value = io.BytesIO(b'{"images":{"secure_base_url":"https://image.tmdb.org/","poster_sizes":["original"]}}')
        code, checks = doctor("--online")
        assert code == 0 and checks["online:tmdb"]["status"] == "ok"
        request = http.call_args.args[0]
        assert request.full_url.startswith("https://api.themoviedb.org/3/configuration?")
        assert not http.call_args.kwargs.get("allow_private", False)
        for error, detail in (
            (urllib.error.HTTPError("https://example.test", 401, secret, {}, None), "HTTP 401"),
            (urllib.error.URLError(TimeoutError(secret)), "timeout"),
        ):
            http.side_effect = error
            code, checks = doctor("--online")
            assert code == 1 and checks["online:tmdb"]["detail"] == detail
        http.side_effect = None
        http.return_value = io.BytesIO(b'{"success":false}')
        code, checks = doctor("--online")
        assert code == 1 and checks["online:tmdb"]["detail"] == "invalid-response"
        http.reset_mock()
        with patch.dict(os.environ, {"MEDIA_DOWNLOADER_OFFLINE": "1"}):
            code, checks = doctor("--online")
            assert code == 0 and checks["online"]["status"] == "skipped"
        data["metadata"]["provider"] = "none"
        doctor("--online")
        http.assert_not_called()


def main():
    with tempfile.TemporaryDirectory(prefix="media-downloader-test.") as temp:
        root = Path(temp)
        version = run([sys.executable, str(SCRIPT), "--version"])
        assert version.stdout.strip() == "Agent Media Pipeline 0.4.8 (config schema 1)"
        root_help = run([sys.executable, str(SCRIPT), "--help"]).stdout
        for command_help in ("List configured defaults", "Show all tasks", "Stop matching owned", "Check tools"):
            assert command_help in root_help, root_help
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
            assert_doctor_contract(module, json.loads(cfg.read_text(encoding="utf-8")))
            env = {**os.environ, "MEDIA_DOWNLOADER_CONFIG": str(cfg), "MEDIA_DOWNLOADER_STATUS_FILE": str(root / "status.json"), "MEDIA_DOWNLOADER_CANDIDATE_FILE": str(root / "candidates.json"), "MEDIA_DOWNLOADER_OFFLINE": "1", "TEST_JACKETT_KEY": "secret-key", "TEST_TORZNAB_KEY": "secret-key"}
            make_video(root / "http" / "retry.mp4")
            retry_config = json.loads(cfg.read_text(encoding="utf-8"))
            retry_config.update(downloadRetries=1, downloadDir=str(root / "retry-delivery"))
            retry_cfg = write_config(root / "retry-config.json", retry_config)
            retry_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(retry_cfg), "MEDIA_DOWNLOADER_STATUS_FILE": str(root / "retry-status.json")}
            retry_command = [sys.executable, str(SCRIPT), "ingest", "Retry Movie", f"http://127.0.0.1:{port}/retry.mp4", "--type", "movie", "--no-archive", "--no-transcode", "--offline"]
            Handler.retry_counts = {}
            Handler.retry_mode = "retry-once"
            run(retry_command, env=retry_env)
            retry_state = next(iter(json.loads((root / "retry-status.json").read_text()).values()))
            assert Handler.retry_counts["/retry.mp4"] == 2
            assert retry_state["phase"] == "done" and not Path(retry_state["workPath"]).exists()
            assert list(Path(retry_state["targetPath"]).glob("*.mp4"))
            Handler.retry_mode = "always-fail"
            for retries in (1, 0):
                retry_config["downloadRetries"] = retries
                write_config(retry_cfg, retry_config)
                Handler.retry_counts = {}
                failed_command = list(retry_command)
                failed_command[3] = f"Failed Retry {retries}"
                run(failed_command, env=retry_env, expect=1)
                assert Handler.retry_counts["/retry.mp4"] == retries + 1
                failed_state = next(state for state in json.loads((root / "retry-status.json").read_text()).values() if state["title"] == f"Failed Retry {retries}")
                assert failed_state["phase"] == "failed" and Path(failed_state["workPath"]).is_dir()
                assert not Path(failed_state["targetPath"]).exists()
            Handler.retry_mode = "success"

            insecure_cfg = root / "insecure-config.json"
            shutil.copy2(cfg, insecure_cfg)
            insecure_cfg.chmod(0o644)
            insecure_doctor = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_CONFIG": str(insecure_cfg)}, expect=1)
            assert "配置文件" in insecure_doctor.stderr and "组/其他权限" in insecure_doctor.stderr
            linked_cfg = root / "linked-config.json"
            linked_cfg.symlink_to(cfg)
            linked_doctor = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_CONFIG": str(linked_cfg)}, expect=1)
            assert "配置文件无法安全读取" in linked_doctor.stderr

            (root / "movie").rmdir()
            doctor = run([sys.executable, str(SCRIPT), "doctor"], env=env)
            doctor_payload = json.loads(doctor.stdout)
            assert doctor_payload["version"] == "0.4.8"
            assert doctor_payload["configSchemaVersion"] == 1
            checks = {item["name"]: item["status"] for item in doctor_payload["checks"]}
            assert checks["work:base"] == "ok"
            assert checks["work:state"] == "ok"
            assert checks["target:tv"] == "ok"
            assert checks["target:movie"] == "unavailable"
            assert any(item["name"] == "ffmpeg" and "version" in item for item in doctor_payload["checks"])
            Handler.last_query = {}
            run([sys.executable, str(SCRIPT), "doctor"], env=env)
            assert Handler.last_query == {}
            online_env = dict(env)
            online_env.pop("MEDIA_DOWNLOADER_OFFLINE", None)
            online_doctor = run([sys.executable, str(SCRIPT), "doctor", "--online"], env=online_env)
            online_checks = {item["name"]: item for item in json.loads(online_doctor.stdout)["checks"]}
            assert online_checks["online:search:jackett"]["status"] == "ok"
            Handler.retry_mode = "caps-fail"
            failed_online = run([sys.executable, str(SCRIPT), "doctor", "--online"], env=online_env, expect=1)
            failed_item = next(item for item in json.loads(failed_online.stdout)["checks"] if item["name"] == "online:search:jackett")
            assert failed_item["status"] == "error" and "secret-key" not in failed_online.stdout
            Handler.retry_mode = "success"
            insecure_cookie = root / "doctor-cookies.txt"
            insecure_cookie.write_text("cookie", encoding="utf-8")
            insecure_cookie.chmod(0o644)
            cookie_result = run([sys.executable, str(SCRIPT), "doctor", "--cookies", str(insecure_cookie)], env=env, expect=1)
            assert "cookies" in cookie_result.stderr
            invalid_retries = json.loads(cfg.read_text(encoding="utf-8"))
            invalid_retries["downloadRetries"] = 1.5
            invalid_retries_cfg = root / "invalid-retries.json"
            write_config(invalid_retries_cfg, invalid_retries)
            bad_retries = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_CONFIG": str(invalid_retries_cfg)}, expect=1)
            assert "downloadRetries" in bad_retries.stderr
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
            write_config(modes_cfg, modes)
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
            write_config(legacy_cfg, modes)
            legacy_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(legacy_cfg)}
            legacy_default = run([sys.executable, str(SCRIPT), "adopt", "Legacy Mode", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--offline", "--dry-run"], env=legacy_env)
            assert json.loads(legacy_default.stdout)["mode"] == "transcode"
            profiles = run([sys.executable, str(SCRIPT), "profiles"], env=modes_env)
            assert json.loads(profiles.stdout)["defaultModes"] == {"tv": "organize", "movie": "transcode"}

            source_cfg = root / "source-config.json"
            write_config(source_cfg, json.loads(cfg.read_text(encoding="utf-8")))
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
            write_config(broken_work_cfg, broken_work)
            broken_doctor = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_CONFIG": str(broken_work_cfg)}, expect=1)
            broken_checks = {item["name"]: item["status"] for item in json.loads(broken_doctor.stdout)["checks"]}
            assert broken_checks["work:base"] == "error"

            # 未配置 downloadDir 时，no-archive 向后兼容：成品留在受控工作区
            no_archive_config = json.loads(cfg.read_text(encoding="utf-8"))
            no_archive_config["targets"] = {}
            no_archive_cfg = root / "no-archive.json"
            write_config(no_archive_cfg, no_archive_config)
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
            write_config(delivery_cfg, delivery_config)
            delivery_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(delivery_cfg)}
            delivery_checks = {item["name"]: item["status"] for item in json.loads(run([sys.executable, str(SCRIPT), "doctor"], env=delivery_env).stdout)["checks"]}
            assert delivery_checks["download:output"] == "ok"
            delivery_url = f"http://127.0.0.1:{port}/Remote.S01E01.mp4"
            delivery_command = [sys.executable, str(SCRIPT), "ingest", "交付电影", delivery_url, "--type", "movie", "--year", "2026", "--no-transcode", "--no-archive", "--offline"]
            delivery_plan = json.loads(run([*delivery_command, "--dry-run"], env=delivery_env).stdout)
            assert delivery_plan["downloadRetries"] == 3
            assert delivery_plan["version"] == "0.4.8" and delivery_plan["configSchemaVersion"] == 1
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
            write_config(playlist_cfg, playlist_config)
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
            write_config(unsafe_delivery_cfg, unsafe_delivery)
            unsafe_delivery_env = {**env, "MEDIA_DOWNLOADER_CONFIG": str(unsafe_delivery_cfg)}
            unsafe_result = run([*delivery_command, "--dry-run"], env=unsafe_delivery_env, expect=1)
            assert "不得位于任务工作区内" in unsafe_result.stderr

            # 无 TMDB key 且 metadata 无海报时 requireArtwork 自动降级，任务可完成
            artwork = json.loads(cfg.read_text(encoding="utf-8"))
            artwork["metadata"] = {"provider": "tmdb", "apiKeyEnv": "UNSET_TEST_TMDB_KEY", "tvFallback": "none", "requireArtwork": True}
            artwork_cfg = root / "artwork.json"
            write_config(artwork_cfg, artwork)
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
            subtitle_plan = json.loads(run([
                sys.executable, str(SCRIPT), "ingest", "Web Subtitles", web_url,
                "--downloader", "yt-dlp", "--type", "tv", "--target", "tv",
                "--write-subs", "--sub-langs", "zh-CN,en", "--dry-run", "--offline",
            ], env=env).stdout)
            assert subtitle_plan["subs"] is True and subtitle_plan["subtitleLanguages"] == "zh-CN,en"
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
                "title": "标题剧", "year": 2026, "seasonNumber": 2,
                "seasonPosterPath": str(root / "source" / "poster.png"),
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
            assert titled_episode.with_suffix(".jpg").is_file()
            assert (titled_episode.parent / "Season02.jpg").is_file()

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
            repair_old.with_name(f"{repair_old.stem}-thumb.jpg").write_bytes(b"legacy thumb")
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
            assert repair_preview["mode"] == "preview" and repair_preview["renameCount"] == 4
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
            assert repaired.with_suffix(".jpg").read_bytes() == b"legacy thumb"
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
            subtitle_probe = run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title:stream=codec_type:stream_tags=language,title,filename,mimetype", "-of", "json", str(mkv_movie)])
            subtitle_payload = json.loads(subtitle_probe.stdout)
            audio_streams = [item for item in subtitle_payload["streams"] if item.get("codec_type") == "audio"]
            subtitle_streams = [item for item in subtitle_payload["streams"] if item.get("codec_type") == "subtitle"]
            attachment_streams = [item for item in subtitle_payload["streams"] if item.get("codec_type") == "attachment"]
            assert "title" not in subtitle_payload.get("format", {}).get("tags", {}), subtitle_payload
            assert audio_streams and audio_streams[0].get("tags") == {"language": "jpn", "title": "日语音轨"}, subtitle_payload
            assert subtitle_streams and subtitle_streams[0].get("tags") == {"language": "chi", "title": "中文"}, subtitle_payload
            assert attachment_streams and attachment_streams[0].get("tags") == {"filename": "font.txt", "mimetype": "text/plain"}, subtitle_payload
            local_subs = run([sys.executable, str(SCRIPT), "adopt", "字幕本地", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--write-subs", "--offline"], env=env, expect=1)
            assert "只适用于 yt-dlp" in local_subs.stderr
            orphan_sub_langs = run([sys.executable, str(SCRIPT), "adopt", "字幕本地", str(root / "source" / "Film.mkv"), "--type", "movie", "--target", "movie", "--sub-langs", "zh-CN", "--offline"], env=env, expect=1)
            assert "必须与 --write-subs" in orphan_sub_langs.stderr

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
            source_stat = source_conflict.stat()
            os.utime(source_conflict, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1))
            changed_retry = run([sys.executable, str(SCRIPT), "adopt", "Conflict", str(source_conflict), "--type", "tv", "--target", "tv", "--offline"], env=env, expect=1)
            assert "来源已变化" in changed_retry.stderr

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
            write_config(overlap_cfg, overlap)
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
