---
name: agent-media-pipeline
description: Discover authorized media through Jackett, Torznab/Prowlarr, or public web sources; review candidates; acquire local files, torrents, direct links, YouTube/Bilibili videos, playlists, and available source subtitles; transcode with TV/movie defaults or preserve the original container and MKV subtitle streams; generate Plex-compatible naming, NFO, show/season/episode artwork, and external subtitles; safely preview or repair existing TV episode names/NFO; deliver locally with cache cleanup; and optionally archive to a local folder, external drive, or NAS. Use for agent-guided media search, download, subtitle handling, organization, metadata, library repair, status, resume, and safe stop workflows.
---

# Agent Media Pipeline

Separate agent judgment from deterministic execution. The agent searches, verifies, compares, and asks for decisions. `run.sh` acquires, transcodes or organizes, writes metadata, validates, archives, and cleans up.

## Safety boundaries

- Handle only sources the user is authorized to access, download, and archive. Never bypass DRM, paywalls, authentication controls, CAPTCHAs, or site restrictions.
- Do not provide infringing sources, circumvention instructions, credentials, or keys.
- Return and compare candidates before downloading unless the user supplied an exact source or explicitly authorized automatic selection.
- Keep API keys, cookies, tokens, and credential-bearing URLs out of commands, configuration, logs, and responses. Store keys only in environment variables. The private `config.json` must be owned by the current user, have no group/other permissions, and must not be a symlink.
- Archival is optional. With `--no-archive`, deliver to configured `downloadDir`; without that setting, retain the Plex-ready folder in the owned workspace for backward compatibility. Archive targets must already exist and be writable; external volumes must be mounted. Never create a fake mount directory.
- Never overwrite an existing different file. Preserve the owned workspace and source after failure.
- Remove a Skill-created workspace only after size, SHA-256, media-validity, and delivery/archive-target checks pass.
- `--reset-work` deletes a verified failed-task workspace. Use it only after the user confirms rebuilding that task.

## Workflow

1. Run `./run.sh --version`, then `./run.sh doctor`. Record the pipeline version/config schema and confirm FFmpeg/FFprobe, the required downloader, and `download:output` when using local delivery. Archive targets are optional; an unused missing target may remain `unavailable`. For YouTube, inspect the reported Deno/EJS checks; `unverified` requires a source `probe` before assuming readiness. Use `doctor --online` to investigate configured provider connectivity, or `doctor --cookies SPEC` to check the selected credential input without opening a browser session.
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
   - Add `--write-subs` (optionally `--sub-langs "zh-CN,en"`) only for yt-dlp web sources that provide subtitles. Never pass `--sub-langs` alone. Unavailable subtitle tracks are skipped; no third-party subtitle service is contacted.
   - A TV file without a season/episode token requires explicit `--season`/`--episode`.
   - Split unsupported multi-episode files before processing.
   - Prefer one task per show: one `adopt` for a local directory, one `ingest --playlist` for a playlist. If episodes must be split, use `--merge` and consistent metadata/profile/target settings. Prefer serial execution for tasks sharing a TV folder to reduce shared artwork/NFO contention; separate shows can run concurrently.
   - When separate tasks add episodes to the same existing TV show, use `--merge`. It keeps existing different artwork (show-level poster/fanart/banner/clearlogo, episode thumbnails, season posters) and `tvshow.nfo`, but still rejects different media, subtitles, and episode NFO files.
   - If aria2 stops after sustained zero traffic, report the stalled candidate and use another reviewed candidate only when the user already authorized fallback or confirms it now.
6. For requested post-download track review, launch a transcode task with `--track-review-seconds 60` and monitor `check` for `track-review`. Report embedded subtitle/audio counts and numbered language/title/codec choices, plus same-stem external subtitle files and their suffixes; filename language labels are hints, not verified language detection. Ask once whether to burn a subtitle or select audio tracks. Check `trackBurnAvailable` before offering burning as ready. Explain the deadline: no reply preserves all audio and all external subtitle copies with the previously selected compression profile. Use `choose-tracks TASK_ID` only during that window and for an explicit selection. Choose exactly one of `--burn-subtitle N`, `--burn-subtitle-lang zh-CN`, or `--burn-sidecar zh.srt` (the exact external suffix from `tracks`); never combine them or supply arbitrary paths. Language selection is resolved independently per file, then batch subtitle language/script/forced consistency is checked before conversion; no match, multiple matches, bitmap-only matches, or known cross-file differences fail before transcoding. `zh`, `zh-CN`, and `chi` are accepted language selectors; title fallback recognizes explicit Chinese labels such as Simplified/简体/简中 and Traditional/繁體/繁中. Generic `zh` may be ambiguous when both scripts exist, so use `zh-CN`/`zh-Hans` or `zh-TW`/`zh-Hant`, or inspect `tracks` and choose an ordinal. Batch ordinal numbers may differ between episodes when selecting by language. Optional `--sub-font`, `--sub-size`, and `--sub-margin-v` adjust hard-burn styling; font family names must not contain FFmpeg filter delimiters, and no OS-specific font is assumed. Do not claim a late choice changed a running conversion or restart without approval. For local media, inspect with `tracks PATH` before launching. An already explicit choice needs no further pause; a file with no embedded/external subtitles and at most one audio track also skips review. Bitmap sidecars are retained but not burned or OCR'd.
   - Malformed language-tag structure, repeated script positions, and unregistered script codes are treated as unknown. This lightweight check does not validate every language/variant registration or prefix restriction; inspect nonstandard tags manually and ask the user or split the batch when equivalence cannot be established.
7. Run `check` until `done`, `failed`, or `stopped`. If `stale: true`, the recorded owned processes are gone; inspect the retained workspace/log before a retry. If `orphaned: true`, an owned downloader still runs without the pipeline parent: use `stop` and confirm termination before retrying. `check` reports these flags without changing saved state. After `stop`, run `check` again.
8. Report the output/archive path, actual mode/profile/naming/target, file count, subtitle/audio counts, any burned track, and unmet requirements.

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
# Optional read-only endpoint checks (skipped under MEDIA_DOWNLOADER_OFFLINE=1)
./run.sh doctor --online
./run.sh profiles
./run.sh sources

# Inspect local soft-subtitle/audio tracks (ordinals start at 0)
./run.sh tracks "/path/to/Movie.mkv"
# Allow a bounded post-download choice; no reply preserves all tracks
./run.sh ingest "Movie Name" "VIDEO_URL" --type movie --no-archive --track-review-seconds 60
# Submit an explicit choice while check reports track-review
./run.sh choose-tracks TASK_ID --burn-subtitle 0 --audio-tracks 0,1
# Or let the pipeline resolve a language separately for each episode
./run.sh adopt "Movie Name" "/path/to/Movie.mkv" --type movie --transcode --no-archive \
  --burn-subtitle-lang zh-CN --sub-size 24 --sub-margin-v 24
# Or select before starting; hard subtitles require video re-encoding
./run.sh adopt "Movie Name" "/path/to/Movie.mkv" --type movie --transcode --no-archive \
  --burn-subtitle 0
# Or burn the same-stem Movie.zh.srt; the external copy is still retained
./run.sh adopt "Movie Name" "/path/to/Movie.mkv" --type movie --transcode --no-archive \
  --burn-sidecar zh.srt

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

Archive performs a complete conflict preflight before copying and rejects an output that contains only sidecars without a video. `--merge` applies to TV only: existing different images (`poster`, `fanart`, `banner`, `clearlogo`, episode thumbnails, season posters) and the root `tvshow.nfo` are logged and kept, wherever they sit inside the show folder. It never weakens no-clobber protection for episode media, subtitles, or episode NFO. `stop` waits for the owned task and downloader/transcoder process group to exit.

## Source behavior

- `auto` selects local for regular local files/directories, aria2 for magnet/`.torrent`/clear direct-media URLs, and yt-dlp for other HTTP(S) pages.
- `add-source` writes only to private, Git-ignored `config.json`, rejects embedded credentials and duplicate names, and requires `--replace` for a confirmed replacement.
- Structured candidates expire after seven days. The agent sees review fields and `candidateId`; the private `0600` cache retains the real download URL.
- Put signed/tokenized URLs in a user-owned regular `0600` `--source-file` rather than a command argument.
- Failed local-file tasks record a source snapshot (path, inode, size, modification time, and directory/sidecar manifest). Single-file snapshots include same-stem subtitles and conventional artwork; if the local source or these sidecars change before retry, the pipeline refuses to reuse the work area; use `--reset-work` only after confirming the new source.
- Playlists are disabled by default. Add `--playlist` only after the user explicitly requests the whole list.
- Use `probe URL` for a single item's formats; use `probe URL --playlist` for the title, count, and ordered entry list. Add the same `--cookies` value when anonymous probing is blocked. Probe output excludes media URLs and cookies.
- Use `--type tv --playlist` for a playlist or Bilibili multi-part collection. For items without recognizable episode tokens, assign episodes in playlist order starting from `--season` (default 1) and `--episode` (default 1). Confirm count and ordering first.
- Omit `--format` for yt-dlp's best available selection. Use a selector such as `bv*[height<=720]+ba/b[height<=720]` for a ceiling, or exact format IDs reported by `probe`. A source selector does not decide pipeline transcoding.
- Let the configured default profile decide the final container; transcode profiles support only MP4 or MKV, and the example/current defaults use MP4. `--no-transcode` preserves downloaded codecs/container. Run `profiles` before naming a profile because private deployments may not contain every example profile.
- `--cookies` takes a supported browser spec (`chrome`, `firefox`, `edge`, `safari`, `brave`, `chromium`, `opera`, `vivaldi`, `whale`) or a current-user-owned `0600` Netscape cookies.txt path. Use it only for the user's authorized session when YouTube bot checks or Bilibili login/quality restrictions require authentication. Do not export, log, copy, or commit cookies, and do not attempt to bypass DRM, CAPTCHA, membership, or regional controls.
- `--write-subs` downloads external manual/auto subtitle tracks with the yt-dlp media, converts them to `srt`, and carries them through organize/transcode as same-stem sidecar files. `--sub-langs` requires `--write-subs`, takes comma-separated codes, and defaults to `metadata.subtitleLanguages`, then metadata language, then `zh-CN`. No third-party subtitle provider is contacted.
- Metadata and remote artwork requests require DNS-resolved public destinations and reject cross-host or HTTPS downgrade redirects; configured API credentials cannot be forwarded to another host. Direct local artwork paths and explicitly configured local indexers remain supported.
- `--no-archive` needs no library target or NAS. Transcoding/organization, NFO, and artwork still run. With `downloadDir`, output is validated and copied to `downloadDir/<Plex folder>` before the owned cache is removed; `--keep-work` retains it. Without `downloadDir`, the Plex-ready folder remains in the owned workspace for backward compatibility. Status `targetPath` always identifies the final output.
- `--no-deliver` implies `--no-archive`, ignores configured `downloadDir` for that task, and retains the Plex-ready folder/workspace. Use it only when the user explicitly wants no transfer and no cleanup of that task workspace.

## Metadata and Plex

Try TMDB through `TMDB_API_KEY`; `provider: "tmdb"` keeps the TVMaze fallback by default, while an omitted provider needs explicit `tvFallback: "tvmaze"`. `metadata.provider: "none"` disables provider lookups, and omitting the metadata block does not trigger an implicit request. When available, fetch the requested season's episode titles/details and use episode-specific IDs in each episode NFO. Without them, continue with minimal NFO and the stable `SxxEyy` filename. A metadata JSON may override or supplement title, original/sort title, year, premiere date, plot, tagline, content rating, rating, runtime, status, genres, countries, tags, studio, directors, writers, actors, external IDs, episode details, and artwork URLs/paths.

Supported root artwork names are `poster`, `fanart`, `banner`, and `clearlogo`. With no TMDB key, still generate minimal valid NFO. If `metadata.requireArtwork=true` but neither a key nor supplied poster exists, warn and downgrade artwork to optional. Otherwise `requireArtwork` is enforced after acquisition and before transcoding: with no `posterPath`/`posterUrl` and no local or downloaded `poster|folder|cover|default|movie` image, the task fails before a full transcode. For a one-off exception use `--allow-missing-artwork`; it skips only the poster requirement for that task and never changes `config.json`. Otherwise supply a poster through `--metadata`, fix the title/year, or turn `requireArtwork` off.

Name Plex episode artwork exactly like the video with only the extension changed to `.jpg`. Prefer an agent-supplied episode `thumbPath`/`thumbUrl`, then TMDB `still_path`. Store a normal season poster as `Season XX/SeasonXX.jpg`, and Season 0 artwork as `season-specials-poster.jpg`. Missing artwork is a warning only.

When adding S02 or later with `--merge`, existing `tvshow.nfo`, poster, and fanart stay untouched, and an existing different episode thumbnail or season poster is kept instead of failing the task; the pipeline adds new episode NFO/artwork/subtitles and the season poster when that slot is free. Do not generate `season.nfo` merely for Plex—it is optional and mostly ignored. Do not maintain legacy root `thumb.png` as a Plex asset.

For TV, the Plex preset may use `{episodeTitleSuffix}`. When a matched metadata episode (or an explicitly confirmed web-playlist item) has a title, name it `Show - S01E03 - Episode title.ext`; otherwise keep `Show - S01E03.ext`. Write that same title to the sibling episode NFO. Keep `tvshow.nfo` show-level only; never duplicate the whole episode catalogue into it.

`repair` uses the same rule for existing libraries: no reliable episode title means no rename. It supports Season 0, moves same-stem subtitles/images/NFO with the episode, refuses duplicate SxxEyy media and existing destinations, and copies plus verifies every new path before removing an old path. It does not remove empty legacy directories. Multi-episode files such as `S01E01-E02` remain intentionally unsupported; split them before ingest or repair.

Verified `metadata.title` controls canonical naming and task identity while status retains the requested title. Enable “Use local Assets” in Plex for local artwork and select Plex NFO Agent on Plex Media Server 1.43.1 or newer. Use the Plex preset for catalogued movies and TV. Prefer a separate Plex “Other Videos” library for unmatched clips or ordinary channel uploads rather than disguising them as film/TV.

Transcoding defaults to MP4 profiles, removes inherited global and chapter metadata, and preserves language/title labels on mapped audio, video, and subtitle tracks. Normalized filenames, NFO, and artwork carry library metadata. `--no-transcode` promises byte-preserving media organization and therefore does not alter embedded metadata.

Inspect downloaded media tracks before processing. Soft subtitles are separate selectable streams, not text already baked into video pixels; already-hardcoded subtitles cannot be detected reliably with FFprobe or extracted as subtitle tracks. Report subtitle/audio counts and each track's ordinal, language, title, codec, and default/forced flags. Unknown language is `und`, not a guessed Chinese/English label.

Without an explicit selection, keep all audio tracks and preserve every soft-subtitle track as an external sidecar alongside the converted video. MKV also retains embedded subtitles and attachments; MP4 uses external subtitles for compatibility. Existing same-stem external subtitles follow the final movie/TV video stem, retain language/forced suffixes, and are copied to the final delivery/archive directory in both modes; verify these final paths, not just cache files. Retain ASS styling and fonts, and disclose subtitle-only Matroska preservation files that are not universally recognized by Plex rather than claiming bitmap subtitles were converted to text. Never silently discard an unextractable subtitle or clean up the source after preservation fails. The final container still comes from the configured profile; nothing forces MP4 or MKV.

Hard subtitles mean burning one selected soft-subtitle track into the picture: playback cannot switch it off, and video re-encoding is required. Do this only after the user explicitly chooses a track. Keep the external copies even when burning a subtitle. Default audio handling maps all tracks and preserves their language/title/disposition labels, using the profile's audio codec/bitrate; this is not byte-identical audio preservation. `organize`/`--no-transcode` retains the original media bytes and embedded streams and cannot burn subtitles or remove audio tracks.

Use `MEDIA_DOWNLOADER_FFMPEG` to explicitly select an executable FFmpeg build when needed; an explicit selection is never bypassed. Ordinary transcodes keep using the configured/PATH FFmpeg. Only when hard-burn is requested (or a track-review window may choose it), if no explicit binary was set and the PATH build lacks `subtitles`, the pipeline may use a known executable candidate (`/opt/homebrew/bin/ffmpeg`, `/usr/local/bin/ffmpeg`, or the tinyMediaManager add-on on macOS); it does not scan arbitrary applications. `doctor` reports the normal FFmpeg separately and identifies an auto-selected burn-capable fallback. If none is usable, the task fails before conversion and lists the probed paths. Text-subtitle burning requires libass and fonts with the required glyphs; filter availability does not prove correct Chinese glyph rendering. Optional `--sub-font`, `--sub-size`, and `--sub-margin-v` let the user choose styling without assuming a platform-specific font. Bitmap tracks remain preserved externally but unsupported burn requests must be reported clearly, not guessed or OCR'd.

## Configuration

Run `cp config.example.json config.json && chmod 600 config.json`. The example uses `$HOME/MediaDownloader`, delivers `--no-archive` output to `$HOME/MediaDownloader/Incoming`, defines no archive targets, and requires no NAS.

- `searchSources`: optional Jackett, generic Torznab/Prowlarr, or web templates; `apiKeyEnv` names an environment variable. Missing keys appear as `optional-missing` in doctor and fail only when that source is actually searched.
- `btStopTimeoutSeconds`: aria2 sustained-zero-traffic limit; default `600`, set `0` only to disable it deliberately.
- `downloadRetries`: finite native downloader retries, integer `0-10`, default `3`; `0` disables retries. aria2/yt-dlp apply this per request/fragment, not as a whole-playlist counter; yt-dlp aborts on unavailable fragments.
- `profiles`: container, resolution, codec, CRF/bitrate, optional target, and naming. The example defaults produce MP4; `mkv` profiles preserve embedded subtitle streams. Inspect private profile names with `profiles` before selecting one.
- `defaultProfiles.tv|movie`: default compression profiles.
- `defaultModes.tv|movie`: `transcode` or `organize`; omitted values remain backward-compatible as `transcode`.
- `downloadDir`: final local destination for `--no-archive`; it is created when its parent is writable, may equal or sit inside `baseDir`, but must never sit inside `.media-downloader-work`.
- `targets`: optional local directory, external-drive, or NAS presets; keep `{}` for local-only use.
- `namingPresets`: TV/movie path templates; `plex` is the default. TV templates can use `{episodeTitle}` or the optional separator-aware `{episodeTitleSuffix}`.
- `metadata`: optional TMDB/TVMaze settings, artwork requirements, and optional comma-separated `subtitleLanguages` default. `provider: "none"` disables lookups; `tvFallback: "tvmaze"` is opt-in when the provider is omitted. `--metadata` paths must exist and be regular JSON files.
- `customWords`: pre-recognition word handling with three arrays. `ignore` removes noise tokens (e.g. `全39集`, `更新至`) from the metadata query; `replace` rewrites tokens (`{"from": "第12话", "to": "E12"}`) before episode parsing; `episodeOffset` shifts episode numbers for split-season/continuous numbering (`{"pattern": "(?i)show-name", "offset": 50}`), where `pattern` matches against `<media_type>:<cleaned title>`. Cleaning affects lookup only; the stored `metadata.title` keeps the supplied title.

For multi-file, non-playlist TV runs, the pipeline logs a `缺集提醒` after metadata when the batch has episode gaps. Single-episode incremental tasks and playlists skip the check to avoid false warnings about episodes outside that task. The report is informational and never fails the run.

Environment overrides include `MEDIA_DOWNLOADER_CONFIG`, `MEDIA_DOWNLOADER_BASE_DIR`, `MEDIA_DOWNLOADER_STATE_DIR`, `MEDIA_DOWNLOADER_DOWNLOAD_DIR`, `MEDIA_DOWNLOADER_TARGET_DIR`, and `MEDIA_DOWNLOADER_OFFLINE=1`.

`stateDir` stores task locks and per-task logs. Global `status.json` and `candidates.json` default to `.runtime/` and can be overridden with `MEDIA_DOWNLOADER_STATUS_FILE` / `MEDIA_DOWNLOADER_CANDIDATE_FILE`.

`--version`, doctor JSON, dry-run plans, and task status expose the pipeline version and configuration schema so an agent can detect stale installations before execution. `doctor --online` is opt-in, checks TMDB `/configuration` and enabled Torznab `t=caps` with bounded requests, and never sends requests when `MEDIA_DOWNLOADER_OFFLINE=1`; `doctor --cookies` validates a private cookies file while browser login remains unverified. A readable caps response does not prove search/download availability or authentication on indexers that serve caps anonymously.

After code or configuration changes, run `./scripts/smoke-test.sh`.
