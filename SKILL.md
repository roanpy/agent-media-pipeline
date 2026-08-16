---
name: agent-media-pipeline
description: Discover authorized media through Jackett, Torznab/Prowlarr, or public web sources; review candidates; acquire local files, torrents, direct links, YouTube/Bilibili videos, or playlists; transcode with TV/movie defaults or preserve the original container; generate Plex-compatible naming, NFO, and artwork; safely preview or repair existing TV episode names/NFO; deliver locally with cache cleanup; and optionally archive to a local folder, external drive, or NAS. Use for agent-guided media search, download, organization, metadata, library repair, status, resume, and safe stop workflows.
---

# Agent Media Pipeline

Separate agent judgment from deterministic execution. The agent searches, verifies, compares, and asks for decisions. `run.sh` acquires, transcodes or organizes, writes metadata, validates, archives, and cleans up.

## Safety boundaries

- Handle only sources the user is authorized to access, download, and archive. Never bypass DRM, paywalls, authentication controls, CAPTCHAs, or site restrictions.
- Do not provide infringing sources, circumvention instructions, credentials, or keys.
- Return and compare candidates before downloading unless the user supplied an exact source or explicitly authorized automatic selection.
- Keep API keys, cookies, tokens, and credential-bearing URLs out of commands, configuration, logs, and responses. Store keys only in environment variables.
- Archival is optional. With `--no-archive`, deliver to configured `downloadDir`; without that setting, retain the Plex-ready folder in the owned workspace for backward compatibility. Archive targets must already exist and be writable; external volumes must be mounted. Never create a fake mount directory.
- Never overwrite an existing different file. Preserve the owned workspace and source after failure.
- Remove a Skill-created workspace only after size, SHA-256, media-validity, and delivery/archive-target checks pass.
- `--reset-work` deletes a verified failed-task workspace. Use it only after the user confirms rebuilding that task.

## Workflow

1. Run `./run.sh --version`, then `./run.sh doctor`. Record the pipeline version/config schema and confirm FFmpeg/FFprobe, the required downloader, and `download:output` when using local delivery. Archive targets are optional; an unused missing target may remain `unavailable`.
2. Establish media type, verified title/year, playlist scope, transcode mode, local-delivery/archive choice and target, profile, naming preset, and quality requirements.
   - Run `profiles` to inspect `defaultModes.tv|movie`.
   - If the user says “use the default,” apply it directly.
   - If the user only supplies media and does not specify type, transcode mode, or archive choice, explain the relevant defaults and ask for the missing decisions.
   - For YouTube/Bilibili, run `probe` before acquisition when the item count, ordering, login requirement, or available quality is uncertain.
3. Discover sources.
   - Run `./run.sh sources` to inspect configured sources.
   - Jackett/Torznab: `./run.sh search "query" --source NAME --type tv|movie --timeout 90` for slow aggregate indexers.
   - Web sources return a `browseUrl`; inspect the public page and extract only an authorized final URL.
   - Accept an explicit local path, magnet, `.torrent`, HTTP(S) direct link, or supported web-media URL.
   - The agent may use browser search for one-off public discovery. Before saving a reusable site, show its name, domain, type, URL template, and credential requirements. Save it with `add-source` only after explicit confirmation. Never silently add sites or secrets to `SKILL.md`, code, or `config.example.json`.
4. Compare title, season/episode, year, resolution, codec, size, publication time, seeders, and source trust. Do not select by seed count alone.
5. After authorization, run `ingest`. Use `--candidate` for cached structured results and a URL for web/direct sources.
   - For YouTube/Bilibili, issue one `ingest --playlist` command. Never run raw yt-dlp and then manually schedule `adopt`; the pipeline must continue through processing, NFO/artwork, delivery/archive, and cleanup under one task/status record.
   - Add `--write-subs` (optionally `--sub-langs "zh-CN,en"`) only for yt-dlp web sources that provide subtitles. Unavailable subtitle tracks are skipped; no third-party subtitle service is contacted.
   - A TV file without a season/episode token requires explicit `--season`/`--episode`.
   - Split unsupported multi-episode files before processing.
   - When separate tasks add episodes to the same existing TV show, use `--merge`. It keeps existing different show-level artwork and `tvshow.nfo`, but still rejects different media, subtitles, and episode NFO files.
   - If aria2 stops after sustained zero traffic, report the stalled candidate and use another reviewed candidate only when the user already authorized fallback or confirms it now.
6. Run `check` until `done` or `failed`. After `stop`, run `check` again.
7. Report the output/archive path, actual mode/profile/naming/target, file count, and unmet requirements.

For an existing TV show folder, run `repair` without `--apply` first and show the complete plan. Pass exactly one show folder, never a TV library/category root. Apply only after approval. Rename only episodes with reliable provider/supplied titles; preserve original paths when titles are missing. Add `--update-nfo` only when the same reliable episode metadata should replace per-episode NFO. `tvshow.nfo` remains untouched.

## Natural-language examples

- “Download this YouTube or Bilibili video, organize it as a movie, use the default transcode mode, and archive it to my movie library.”
- “Download this playlist as TV season 1 starting at episode 1, preserve the containers, deliver it to my download directory, and clean the cache.”
- “Process this local episode folder, complete titles, NFO, poster, and fanart, then organize it for Plex.”
- “Search for authorized sources for this show and show me candidates before downloading.”
- “Process this with the defaults.” Use `defaultModes.tv|movie` without another mode question once the media type is known.

Run `./run.sh --help` or a command-specific `--help` for the CLI contract.

## Commands

```bash
# Diagnose and inspect configuration
./run.sh --version
./run.sh doctor
./run.sh profiles
./run.sh sources

# Save a reviewed reusable source to private config.json; store env-var names, never keys
./run.sh add-source public-site 'https://example.test/search?q={query}' --type web
./run.sh add-source prowlarr 'http://127.0.0.1:9696/1/api' --type torznab \
  --api-key-env PROWLARR_API_KEY

# Search and use a cached structured candidate
./run.sh search "Show Name S01" --source jackett --type tv --timeout 90
./run.sh ingest "Show Name" --candidate CANDIDATE_ID --type tv --year 2026 \
  --profile tv1080 --target tv-library --naming plex --merge

# Use a final web-media URL
./run.sh probe "https://authorized.example/video"
./run.sh ingest "Movie Name" "https://authorized.example/video" --type movie \
  --downloader yt-dlp --profile movie1080 --target movie-library

# Inspect and map an explicitly approved YouTube/Bilibili playlist to sequential TV episodes
./run.sh probe "PLAYLIST_URL" --playlist
./run.sh ingest "Course Name" "PLAYLIST_URL" --type tv --downloader yt-dlp \
  --playlist --season 1 --episode 1 --no-transcode

# Select source quality and use the user's authorized browser session when the site requires login
./run.sh ingest "Course Name" "PLAYLIST_URL" --type tv --downloader yt-dlp \
  --playlist --format "bv*[height<=720]+ba/b[height<=720]" --cookies chrome

# Download provider subtitles with a web playlist when the source offers them
./run.sh ingest "Course Name" "PLAYLIST_URL" --type tv --downloader yt-dlp \
  --playlist --write-subs --sub-langs "zh-CN,en"

# Process locally without any target configuration or archive transfer
./run.sh ingest "Course Name" "VIDEO_URL" --type tv --downloader yt-dlp \
  --season 1 --episode 1 --no-archive

# Process but do not archive or deliver; keep the finished folder in the owned workspace
./run.sh ingest "Course Name" "VIDEO_URL" --type tv --downloader yt-dlp \
  --no-transcode --no-deliver

# Keep a signed/tokenized URL out of process arguments
chmod 600 /private/tmp/source-url
./run.sh ingest "Movie Name" --source-file /private/tmp/source-url --type movie

# Adopt manually downloaded media; SxxExx is inferred when present
./run.sh adopt "Show Name" "/path/to/files" --type tv --year 2026 \
  --metadata "/path/to/metadata.json"

# Override TV/movie defaults
./run.sh adopt "Show Name" "/path/to/files" --type tv --transcode
./run.sh adopt "Movie Name" "/path/to/Movie.mkv" --type movie --no-transcode

# Explicitly update existing NFO only; media, subtitles, and artwork remain protected
./run.sh adopt "Show Name" "/path/to/Show.S01E02.mkv" --type tv --year 2026 \
  --metadata "/path/to/metadata.json" --update-nfo

# Preview an existing single-show folder, then apply the reviewed episode-title/NFO repair
./run.sh repair "Show Name" "/path/to/Show Name (2026)" --year 2026 --season 1 \
  --metadata "/path/to/metadata.json" --naming plex
./run.sh repair "Show Name" "/path/to/Show Name (2026)" --year 2026 --season 1 \
  --metadata "/path/to/metadata.json" --naming plex --update-nfo --apply

# Organize without transcoding; preserve the original container
./run.sh organize "Show Name" "/path/to/Show.S01E02.mkv" --type tv \
  --metadata "/path/to/metadata.json" --target tv-library

# Preview, inspect, and stop
./run.sh ingest "Show Name" "magnet:?xt=urn:btih:..." --type tv --dry-run
./run.sh check "Show Name (2026)"
./run.sh stop "Show Name (2026)"
```

`resume` and `download` alias `ingest`; `process` aliases `adopt`. `organize` skips profile transcoding but still uses profile naming and an optional default target. `repair` is foreground and preview-only unless `--apply` is explicit. Long-running pipeline commands launch in the background by default; add `--foreground` while debugging.

Archive performs a complete conflict preflight before copying. `--merge` is intentionally narrow: for TV only, existing different root `poster`, `fanart`, `banner`, `clearlogo`, and `tvshow.nfo` are logged and kept. It never weakens no-clobber protection for episode media, subtitles, or episode NFO. `stop` waits for the owned task and downloader/transcoder process group to exit.

## Source behavior

- `auto` selects local for regular local files/directories, aria2 for magnet/`.torrent`/clear direct-media URLs, and yt-dlp for other HTTP(S) pages.
- `add-source` writes only to private, Git-ignored `config.json`, rejects embedded credentials and duplicate names, and requires `--replace` for a confirmed replacement.
- Structured candidates expire after seven days. The agent sees review fields and `candidateId`; the private `0600` cache retains the real download URL.
- Put signed/tokenized URLs in a user-owned regular `0600` `--source-file` rather than a command argument.
- Playlists are disabled by default. Add `--playlist` only after the user explicitly requests the whole list.
- Use `probe URL` for a single item's formats; use `probe URL --playlist` for the title, count, and ordered entry list. Add the same `--cookies` value when anonymous probing is blocked. Probe output excludes media URLs and cookies.
- Use `--type tv --playlist` for a playlist or Bilibili multi-part collection. For items without recognizable episode tokens, assign episodes in playlist order starting from `--season` (default 1) and `--episode` (default 1). Confirm count and ordering first.
- Omit `--format` for yt-dlp's best available selection. Use a selector such as `bv*[height<=720]+ba/b[height<=720]` for a ceiling, or exact format IDs reported by `probe`. A source selector does not decide pipeline transcoding.
- Default `transcode` profiles produce final MP4 regardless of the downloaded container. `--no-transcode` preserves the downloaded codecs/container; if MP4 is mandatory, use the MP4 transcode profile rather than assuming a web source provides MP4.
- `--cookies` takes a supported browser spec (`chrome`, `firefox`, `edge`, `safari`, `brave`, `chromium`, `opera`, `vivaldi`, `whale`) or a current-user-owned `0600` Netscape cookies.txt path. Use it only for the user's authorized session when YouTube bot checks or Bilibili login/quality restrictions require authentication. Do not export, log, copy, or commit cookies, and do not attempt to bypass DRM, CAPTCHA, membership, or regional controls.
- `--write-subs` downloads external manual/auto subtitle tracks with the yt-dlp media, prefers `srt`, and carries them through organize/transcode as same-stem sidecar files. `--sub-langs` takes comma-separated codes and defaults to the metadata language or `zh-CN`. No subtitles are ever fetched from third-party subtitle providers, so a source without subtitle tracks simply stays unsubtitled.
- `--no-archive` needs no library target or NAS. Transcoding/organization, NFO, and artwork still run. With `downloadDir`, output is validated and copied to `downloadDir/<Plex folder>` before the owned cache is removed; `--keep-work` retains it. Without `downloadDir`, the Plex-ready folder remains in the owned workspace for backward compatibility. Status `targetPath` always identifies the final output.
- `--no-deliver` implies `--no-archive`, ignores configured `downloadDir` for that task, and retains the Plex-ready folder/workspace. Use it only when the user explicitly wants no transfer and no cleanup of that task workspace.

## Metadata and Plex

Try TMDB through `TMDB_API_KEY`; TV may fall back to TVMaze. Both providers are optional. When available, fetch the requested season's episode titles/details and use episode-specific IDs in each episode NFO. Without them, continue with minimal NFO and the stable `SxxEyy` filename. A metadata JSON may override or supplement title, original/sort title, year, premiere date, plot, tagline, content rating, rating, runtime, status, genres, countries, tags, studio, directors, writers, actors, external IDs, episode details, and artwork URLs/paths.

Supported root artwork names are `poster`, `fanart`, `banner`, and `clearlogo`. With no TMDB key, still generate minimal valid NFO. If `metadata.requireArtwork=true` but neither a key nor supplied poster exists, warn and downgrade artwork to optional.

Per-episode thumbnails use the Plex/Kodi `-thumb.jpg` convention. The pipeline prefers an agent-supplied `thumbPath`/`thumbUrl` on the episode record, then TMDB's episode `still_path`. A missing episode still is a warning only and never fails a task.

For TV, the Plex preset may use `{episodeTitleSuffix}`. When a matched metadata episode (or an explicitly confirmed web-playlist item) has a title, name it `Show - S01E03 - Episode title.ext`; otherwise keep `Show - S01E03.ext`. Write that same title to the sibling episode NFO. Keep `tvshow.nfo` show-level only; never duplicate the whole episode catalogue into it.

`repair` uses the same rule for existing libraries: no reliable episode title means no rename. It supports Season 0, moves same-stem subtitles/images/NFO with the episode, refuses duplicate SxxEyy media and existing destinations, and copies plus verifies every new path before removing an old path. It does not remove empty legacy directories. Multi-episode files such as `S01E01-E02` remain intentionally unsupported; split them before ingest or repair.

Verified `metadata.title` controls canonical naming and task identity while status retains the requested title. Enable “Use local Assets” in Plex for local artwork and select Plex NFO Agent on Plex Media Server 1.43.1 or newer. Use the Plex preset for catalogued movies and TV. Prefer a separate Plex “Other Videos” library for unmatched clips or ordinary channel uploads rather than disguising them as film/TV.

Transcoding defaults to MP4 profiles and removes inherited global and chapter metadata. Normalized filenames, NFO, and artwork carry library metadata. `--no-transcode` promises byte-preserving media organization and therefore does not alter embedded metadata.

When a profile uses `container: "mkv"`, transcoding keeps the source's embedded subtitle streams unchanged (`-c:s copy`). MP4 containers still drop embedded subtitles for compatibility; same-stem external subtitle files always survive both modes. The default container is whatever the configured default profile declares; nothing forces MP4.

## Configuration

Run `cp config.example.json config.json && chmod 600 config.json`. The example uses `$HOME/MediaDownloader`, delivers `--no-archive` output to `$HOME/MediaDownloader/Incoming`, defines no archive targets, and requires no NAS.

- `searchSources`: optional Jackett, generic Torznab/Prowlarr, or web templates; `apiKeyEnv` names an environment variable. Missing keys appear as `optional-missing` in doctor and fail only when that source is actually searched.
- `btStopTimeoutSeconds`: aria2 sustained-zero-traffic limit; default `600`, set `0` only to disable it deliberately.
- `profiles`: container, resolution, codec, CRF/bitrate, optional target, and naming. The example defaults produce MP4; `mkv` profiles also preserve embedded subtitle streams during transcode.
- `defaultProfiles.tv|movie`: default compression profiles.
- `defaultModes.tv|movie`: `transcode` or `organize`; omitted values remain backward-compatible as `transcode`.
- `downloadDir`: final local destination for `--no-archive`; it is created when its parent is writable, may equal or sit inside `baseDir`, but must never sit inside `.media-downloader-work`.
- `targets`: optional local directory, external-drive, or NAS presets; keep `{}` for local-only use.
- `namingPresets`: TV/movie path templates; `plex` is the default. TV templates can use `{episodeTitle}` or the optional separator-aware `{episodeTitleSuffix}`.
- `metadata`: TMDB, TVMaze fallback, and artwork requirements.
- `customWords`: pre-recognition word handling with three arrays. `ignore` removes noise tokens (e.g. `全39集`, `更新至`) from the metadata query; `replace` rewrites tokens (`{"from": "第12话", "to": "E12"}`) before episode parsing; `episodeOffset` shifts episode numbers for split-season/continuous numbering (`{"pattern": "(?i)show-name", "offset": 50}`), where `pattern` matches against `<media_type>:<cleaned title>`. Cleaning affects lookup only; the stored `metadata.title` keeps the supplied title.

For TV runs, the pipeline logs a `缺集提醒` after metadata when the covered seasons have episode gaps. It reports against TMDB/TVMaze episode lists when present, otherwise against the contiguous range up to the highest fetched episode. Playlist mode skips the check. The report is informational and never fails the run.

Environment overrides include `MEDIA_DOWNLOADER_CONFIG`, `MEDIA_DOWNLOADER_BASE_DIR`, `MEDIA_DOWNLOADER_STATE_DIR`, `MEDIA_DOWNLOADER_DOWNLOAD_DIR`, `MEDIA_DOWNLOADER_TARGET_DIR`, and `MEDIA_DOWNLOADER_OFFLINE=1`.

`stateDir` stores task locks and per-task logs. Global `status.json` and `candidates.json` default to `.runtime/` and can be overridden with `MEDIA_DOWNLOADER_STATUS_FILE` / `MEDIA_DOWNLOADER_CANDIDATE_FILE`.

`--version`, doctor JSON, dry-run plans, and task status expose the pipeline version and configuration schema so an agent can detect stale installations before execution.

After code or configuration changes, run `./scripts/smoke-test.sh`.
