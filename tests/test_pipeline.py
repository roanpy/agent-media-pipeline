#!/usr/bin/env python3
"""One dependency-free integration check for the full media ingest contract."""

from __future__ import annotations

import contextlib
import fcntl
import http.server
import importlib.util
import json
import os
import socketserver
import stat
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "media-downloader.py"


def run(command, *, env=None, expect=0):
    result = subprocess.run(command, cwd=PROJECT, env=env, capture_output=True, text=True)
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
        "defaultNaming": "plex",
        "targets": {"tv": {"path": str(root / "tv")}, "movie": {"path": str(root / "movie")}},
        "namingPresets": {
            "plex": {
                "tv": {"showDir": "{canonical}", "seasonDir": "Season {season:02d}", "episodeFile": "{canonical} - S{season:02d}E{episode:02d}.{ext}"},
                "movie": {"showDir": "{canonical}", "movieFile": "{canonical}.{ext}"},
            }
        },
        "profiles": {
            "tv": {"type": "tv", "container": "mp4", "resolution": 90, "videoCodec": "libx264", "crf": 28, "audioCodec": "aac", "audioBitrate": "64k", "preset": "ultrafast", "target": "tv"},
            "movie": {"type": "movie", "container": "mp4", "resolution": 90, "videoCodec": "libx264", "crf": 28, "audioCodec": "aac", "audioBitrate": "64k", "preset": "ultrafast", "target": "movie"},
        },
        "searchSources": {
            "jackett": {"type": "jackett", "enabled": True, "url": f"http://127.0.0.1:{port}", "indexer": "all", "apiKeyEnv": "TEST_JACKETT_KEY"},
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

    def unsupported_link(_source_path, _target_path):
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

    symlink_target = root / "atomic-symlink.txt"
    symlink_target.symlink_to(source)
    assert not module.existing_matches(source, symlink_target, 0)
    return module


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

    canonical_id = module.task_id("movie", "测试电影 (2025)", root / "movie")
    assert canonical_id == module.task_id("movie", "测试电影 (2025)", root / "movie")
    assert canonical_id != module.task_id("movie", "测试电影 (2025)", root / "other-movie")
    try:
        module.sanitize_component("剧" * 100)
    except ValueError as exc:
        assert "字节" in str(exc)
    else:
        raise AssertionError("overlong UTF-8 path components must be rejected")

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
    finally:
        module.validate_video = original_validate


def main():
    with tempfile.TemporaryDirectory(prefix="media-downloader-test.") as temp:
        root = Path(temp)
        module = assert_atomic_copy_never_overwrites(root)
        assert_path_and_naming_guards(module, root)
        for name in ("tv", "movie", "source", "http"):
            (root / name).mkdir()
        make_video(root / "source" / "Example.S02E03.mkv")
        make_video(root / "source" / "Film.mkv")
        make_video(root / "source" / "Organize.Show.S04E05.mkv")
        make_video(root / "source" / "Organize.Movie.mkv")
        make_video(root / "http" / "Remote.S01E01.mp4")
        make_image(root / "source" / "poster.png")
        (root / "source" / "Example.S02E03.zh.forced.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
        metadata = root / "metadata.json"
        metadata.write_text(json.dumps({
            "title": "示例剧", "originalTitle": "Example Show", "year": 2026,
            "plot": "测试剧情", "ids": {"tmdb": 123}, "posterPath": str(root / "source" / "poster.png"),
            "episodes": [{"season": 2, "episode": 3, "title": "第三集", "plot": "单集剧情"}],
        }, ensure_ascii=False), encoding="utf-8")
        movie_metadata = root / "movie-metadata.json"
        movie_metadata.write_text(json.dumps({
            "title": "测试电影", "originalTitle": "Test Film", "year": 2025,
            "plot": "电影剧情", "posterPath": str(root / "source" / "poster.png"),
        }, ensure_ascii=False), encoding="utf-8")

        with server(root / "http") as port:
            cfg = config(root, port)
            env = {**os.environ, "MEDIA_DOWNLOADER_CONFIG": str(cfg), "MEDIA_DOWNLOADER_STATUS_FILE": str(root / "status.json"), "MEDIA_DOWNLOADER_CANDIDATE_FILE": str(root / "candidates.json"), "MEDIA_DOWNLOADER_OFFLINE": "1", "TEST_JACKETT_KEY": "secret-key"}

            (root / "movie").rmdir()
            doctor = run([sys.executable, str(SCRIPT), "doctor"], env=env)
            checks = {item["name"]: item["status"] for item in json.loads(doctor.stdout)["checks"]}
            assert checks["target:tv"] == "ok"
            assert checks["target:movie"] == "unavailable"
            (root / "movie").mkdir()
            env_target = run([sys.executable, str(SCRIPT), "doctor"], env={**env, "MEDIA_DOWNLOADER_TARGET_DIR": str(root / "tv")})
            env_checks = {item["name"]: item["status"] for item in json.loads(env_target.stdout)["checks"]}
            assert env_checks["target:environment"] == "ok"

            searched = run([sys.executable, str(SCRIPT), "search", "Remote", "--source", "jackett", "--type", "tv"], env=env)
            payload = json.loads(searched.stdout)
            assert len(payload["candidates"]) == 1
            assert "downloadUrl" not in payload["candidates"][0]
            assert Handler.last_query["apikey"] == ["secret-key"]
            candidate = payload["candidates"][0]["candidateId"]

            run([sys.executable, str(SCRIPT), "ingest", "Remote", "--candidate", candidate, "--type", "tv", "--target", "tv", "--offline"], env=env)
            remote = root / "tv" / "Remote" / "Season 01" / "Remote - S01E01.mp4"
            assert remote.is_file()

            web_url = f"http://127.0.0.1:{port}/Remote.S01E01.mp4"
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
            episode_digest = episode.read_bytes()
            poster_digest = (show / "poster.jpg").read_bytes()
            metadata.write_text(json.dumps({
                "title": "示例剧", "originalTitle": "Example Show", "year": 2026,
                "plot": "更新后的剧集简介", "ids": {"tmdb": 123}, "posterPath": str(root / "source" / "poster.png"),
                "episodes": [{"season": 2, "episode": 3, "title": "修正后的第三集", "plot": "更新后的单集简介"}],
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

            source_conflict = root / "source" / "Conflict.S01E01.mkv"
            make_video(source_conflict)
            conflict_target = root / "tv" / "Conflict" / "Season 01" / "Conflict - S01E01.mp4"
            conflict_target.parent.mkdir(parents=True)
            conflict_target.write_bytes(b"different")
            conflict = run([sys.executable, str(SCRIPT), "adopt", "Conflict", str(source_conflict), "--type", "tv", "--target", "tv", "--offline"], env=env, expect=1)
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
            movie_state = next(item for item in json.loads((root / "status.json").read_text(encoding="utf-8")).values() if item.get("requestedTitle") == "待核实电影")
            assert movie_state["title"] == "测试电影 (2025)"
            task_log = Path(movie_state["logPath"])
            assert task_log.resolve().is_relative_to((root / "state").resolve())
            assert stat.S_IMODE(task_log.stat().st_mode) == 0o600

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
            assert {item["phase"] for item in statuses.values()} == {"done", "failed"}

    print("integration test passed")


if __name__ == "__main__":
    import sys
    main()
