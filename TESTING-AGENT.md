# Agent Testing Brief — agent-media-pipeline

Audience: an AI agent (Hermes) running verification. Work in a clone or the repo at `/Users/peter/Developer/media-downloader`. Never touch real NAS paths, `/Volumes/*`, or `config.json`; use only temp dirs and local fakes. Do not modify tracked files.

## Environment

- macOS/Linux, Python 3.9+ (verified on 3.9.6; `from __future__ import annotations` defers PEP 604 unions), `ffmpeg`+`ffprobe` required, `aria2c`+`yt-dlp` optional.
- All overrides via env: `MEDIA_DOWNLOADER_CONFIG`, `MEDIA_DOWNLOADER_BASE_DIR`, `MEDIA_DOWNLOADER_STATE_DIR`, `MEDIA_DOWNLOADER_STATUS_FILE`, `MEDIA_DOWNLOADER_DOWNLOAD_DIR`, `MEDIA_DOWNLOADER_OFFLINE=1`.
- Build a minimal config with `minMediaDurationSeconds: 0`, `metadata: {"provider":"none","tvFallback":"none","requireArtwork":false}`, and `targets: {}`.
- Generate fixtures with `ffmpeg -f lavfi -i testsrc2=... -f lavfi -i sine=... -t 3 -c:v libx264 -c:a aac out.mkv`. Serve HTTP fixtures with `python3 -m http.server`.

## Pass criteria (run each, report pass/fail + evidence)

1. **Baseline**: `./scripts/smoke-test.sh` prints `integration test passed`; `ruff check media-downloader.py tests/test_pipeline.py` clean; `python3 -m py_compile` both files OK.
2. **Movie no-archive → downloadDir**: `adopt` a local mkv with `--type movie --no-archive --offline --metadata <path-to-json>` (a file containing `{"title": ..., "year": ...}`). Expect `downloadDir/<Title> (<Year>)/<Title> (<Year>).<ext>` + `movie.nfo`; work area removed; `status.json` `targetPath` points at the delivered folder.
3. **TV episode naming**: adopt `Show.S02E03.mkv` as TV → `.../Season 02/<Show> - S02E03.<ext>` and a sibling `.nfo` containing `<season>2</season>`, `<episode>3</episode>`, and at least one `<uniqueid>`.
4. **Organize (no transcode)**: `organize` keeps the original container bytes (compare SHA-256 source vs output) and still writes NFO.
5. **Long magnet**: pass a >255-char `magnet:?xt=urn:btih:...` via `--source-file` (0600) with `--no-archive --dry-run`; expect a plan, NOT `[Errno 63] File name too long`. (`--no-archive` is required here because the test config sets `targets: {}`; otherwise the dry-run stops at "未配置目标预设".)
6. **TMDB year strip (stubbed)**: stub HTTP and confirm `fetch_tmdb` with title `"Name 2026"` queries `query=Name` + `year=2026`. Skip if stubbing is out of scope.
7. **No-clobber**: re-run an adopt whose NFO/metadata differs from the existing output; the run must fail with `拒绝覆盖` and leave the first output byte-identical. (A byte-identical re-run is intentionally idempotent and exits 0 — only differing content is rejected.)
8. **--keep-work**: with it, the delivered copy exists AND the work area is retained.
9. **Safety negatives**: `--source-file` with 0644 perms is rejected; a `downloadDir` inside `.media-downloader-work` is rejected.
10. **doctor**: `./run.sh doctor` returns valid JSON; `download:output` is `ok` when `downloadDir` is set.

## Report format

Table of test → pass/fail → key evidence (paths created, NFO snippet, error text). List any unexpected stderr, leftover temp files, or processes. Clean up all temp dirs and kill any background jobs when done.
