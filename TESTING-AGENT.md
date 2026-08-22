# Agent Testing Brief — agent-media-pipeline

Audience: an independent AI verification agent. Work from the repository root or an isolated clone. Never touch real NAS paths, `/Volumes/*`, or `config.json`; use only temp dirs and local fakes. Do not modify tracked files.

## Environment

- macOS/Linux, Python 3.9+ (verified on 3.9.6; `from __future__ import annotations` defers PEP 604 unions), `ffmpeg`+`ffprobe` required, `aria2c`+`yt-dlp` optional.
- All overrides via env: `MEDIA_DOWNLOADER_CONFIG`, `MEDIA_DOWNLOADER_BASE_DIR`, `MEDIA_DOWNLOADER_STATE_DIR`, `MEDIA_DOWNLOADER_STATUS_FILE`, `MEDIA_DOWNLOADER_DOWNLOAD_DIR`, `MEDIA_DOWNLOADER_OFFLINE=1`.
- Build a minimal config with `minMediaDurationSeconds: 0`, `metadata: {"provider":"none","tvFallback":"none","requireArtwork":false}`, and `targets: {}`.
- Generate fixtures with `ffmpeg -f lavfi -i testsrc2=... -f lavfi -i sine=... -t 3 -c:v libx264 -c:a aac out.mkv`. Serve HTTP fixtures with `python3 -m http.server`.

## Pass criteria (run each, report pass/fail + evidence)

1. **Baseline/version**: `./run.sh --version` prints `Agent Media Pipeline 0.4.4 (config schema 1)`; `./scripts/smoke-test.sh` prints `integration test passed`; `ruff check media-downloader.py tests/test_pipeline.py` clean; `python3 -m py_compile` both files OK.
2. **Movie no-archive → downloadDir**: `adopt` a local mkv with `--type movie --no-archive --offline --metadata <path-to-json>` (a file containing `{"title": ..., "year": ...}`). Expect `downloadDir/<Title> (<Year>)/<Title> (<Year>).<ext>` + `movie.nfo`; work area removed; `status.json` `targetPath` points at the delivered folder.
3. **TV episode naming**: adopt `Show.S02E03.mkv` as TV → `.../Season 02/<Show> - S02E03.<ext>` and a sibling `.nfo` containing `<season>2</season>`, `<episode>3</episode>`, and at least one `<uniqueid>`.
4. **Organize (no transcode)**: `organize` keeps the original container bytes (compare SHA-256 source vs output) and still writes NFO.
5. **Long magnet**: pass a >255-char `magnet:?xt=urn:btih:...` via `--source-file` (0600). First use `--no-archive --dry-run` and expect a plan; then run without `--dry-run` against a stubbed/fake `aria2c` and confirm the `acquire` branch receives it through `--input-file`, never `Path.exists()`. Neither run may emit `[Errno 63] File name too long`. (`--no-archive` is required because the test config sets `targets: {}`.)
6. **TMDB year strip (stubbed)**: stub HTTP and confirm `fetch_tmdb` with title `"Name 2026"` queries `query=Name` + `year=2026`. Skip if stubbing is out of scope.
7. **No-clobber**: re-run an adopt whose NFO/metadata differs from the existing output; the run must fail with `拒绝覆盖` and leave the first output byte-identical. (A byte-identical re-run is intentionally idempotent and exits 0 — only differing content is rejected.)
8. **--keep-work**: with it, the delivered copy exists AND the work area is retained.
9. **Safety negatives**: `--source-file` with 0644 perms and HTTP URLs with embedded credentials are rejected; a `downloadDir` inside `.media-downloader-work` is rejected; config files that are group/other-readable or symlinked are rejected; a failed local task detects changed file/directory snapshots before retry.
10. **doctor**: `./run.sh doctor` returns valid JSON with `version: 0.4.4` and `configSchemaVersion: 1`; `download:output` is `ok` when `downloadDir` is set.
11. **Incremental TV merge**: pre-create a different root `fanart.jpg`/`tvshow.nfo`, then add an S02 episode. Without `--merge`, expect failure before any episode is copied. Re-run the same task with `--merge`; expect the new episode/NFO, unchanged shared files, and a `合并跳过已有共享文件` log. A different existing episode media file must still fail under `--merge`.
12. **Search/stall controls**: `search --timeout 90` succeeds against the stub; values outside 1-300 fail. Capture fake aria2 arguments and require `--bt-stop-timeout=600` by default.
13. **Stop closure**: run a fake long-lived aria2 child in its own process group, call `stop`, and require parent + child exit and final `phase: stopped` with no leftover process.
14. **YouTube/Bilibili contract (stub/local HTTP only)**: `probe` must return redacted JSON (`kind`, formats for one video; count/order for a stubbed playlist). Capture yt-dlp args and require `--no-playlist` by default, `--yes-playlist` only with `--playlist`, output prefix `%(playlist_index,autonumber)03d`, exact `--format` forwarding, and browser/file cookie routing. Reject an insecure cookies.txt and reject `--playlist --type movie`. Do not contact public sites or use real browser cookies in automated tests.
15. **Existing TV repair**: create one temporary show folder with `tvshow.nfo`, `Season 00/Legacy.S00E01.mkv`, same-stem subtitle/NFO, and an untitled S00E02. Supply metadata only for E01. `repair` without `--apply` must change nothing and preview the titled target; passing the parent library root must fail. `repair --apply --update-nfo` must move E01/sidecars to the titled Plex name, update its episode NFO/ID, preserve E02 and `tvshow.nfo`, reject collisions, and be idempotent on a second preview.
16. **Episode/season artwork**: a TV adopt whose episode metadata supplies `thumbPath` must produce an identically named `.jpg` beside the episode, never `-thumb.jpg`. A supplied/TMDB season poster must produce `Season 02/Season02.jpg` (`season-specials-poster.jpg` for Season 0). Missing artwork only warns. `repair` migrates a legacy same-stem `-thumb.jpg` to the official identical-base `.jpg` when renaming that episode.
17. **Subtitles**: a stubbed yt-dlp acquire with `--write-subs --sub-langs zh-CN,en` must include `--write-subs --write-auto-subs --sub-format srt/best --sub-langs zh-CN,en --convert-subs srt`; invalid/empty language selections are rejected. `--sub-langs` without `--write-subs` and subtitle flags on non-yt-dlp sources must fail. Dry-run/status expose the resolved languages, and changing format/cookies/subtitle settings changes the source fingerprint. External same-stem `.srt` files must survive organize/transcode.
18. **Stream metadata policy**: a movie profile with `container: "mkv"` must retain mapped video/audio/subtitle stream `language/title` tags and Matroska attachment metadata while dropping inherited global/chapter metadata; embedded subtitles remain present (no `-sn`), and a missing mapped stream must fail validation. The MP4 profile still drops embedded subtitles/attachments and keeps external sidecars.

## Report format

Table of test → pass/fail → key evidence (paths created, NFO snippet, error text). List any unexpected stderr, leftover temp files, or processes. Clean up all temp dirs and kill any background jobs when done.
