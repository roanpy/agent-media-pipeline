---
name: agent-media-pipeline
description: Discover authorized media through Jackett, Torznab/Prowlarr, or public web sources; review candidates; acquire local files, torrents, direct links, YouTube/Bilibili videos, or playlists; transcode with TV/movie defaults or preserve the original container; generate Plex-compatible naming, NFO, and artwork; and optionally archive to a local folder, external drive, or NAS. Use for agent-guided media search, download, organization, metadata, status, resume, and safe stop workflows.
---

# Media Downloader

Separate agent judgment from deterministic execution. The agent searches, verifies, compares, and asks for decisions. `run.sh` acquires, transcodes or organizes, writes metadata, validates, archives, and cleans up.

## Safety boundaries

- Handle only sources the user is authorized to access, download, and archive. Never bypass DRM, paywalls, authentication controls, CAPTCHAs, or site restrictions.
- Do not provide infringing sources, circumvention instructions, credentials, or keys.
- Return and compare candidates before downloading unless the user supplied an exact source or explicitly authorized automatic selection.
- Keep API keys, cookies, tokens, and credential-bearing URLs out of commands, configuration, logs, and responses. Store keys only in environment variables.
- Archival is optional. When enabled, the local, external-drive, or NAS target must already exist and be writable; external volumes must be mounted. Never create a fake mount directory.
- Never overwrite an existing different file. Preserve the owned workspace and source after failure.
- Remove a Skill-created workspace only after size, SHA-256, media-validity, and archive-target checks pass.
- `--reset-work` deletes a verified failed-task workspace. Use it only after the user confirms rebuilding that task.

## Workflow

1. Run `./run.sh doctor`. Confirm FFmpeg/FFprobe and the required downloader. Archive targets are optional; an unused missing target may remain `unavailable`.
2. Establish media type, verified title/year, playlist scope, transcode mode, archive choice/target, profile, naming preset, and quality requirements.
   - Run `profiles` to inspect `defaultModes.tv|movie`.
   - If the user says “use the default,” apply it directly.
   - If the user only supplies media and does not specify type, transcode mode, or archive choice, explain the relevant defaults and ask for the missing decisions.
3. Discover sources.
   - Run `./run.sh sources` to inspect configured sources.
   - Jackett/Torznab: `./run.sh search "query" --source NAME --type tv|movie`.
   - Web sources return a `browseUrl`; inspect the public page and extract only an authorized final URL.
   - Accept an explicit local path, magnet, `.torrent`, HTTP(S) direct link, or supported web-media URL.
   - The agent may use browser search for one-off public discovery. Before saving a reusable site, show its name, domain, type, URL template, and credential requirements. Save it with `add-source` only after explicit confirmation. Never silently add sites or secrets to `SKILL.md`, code, or `config.example.json`.
4. Compare title, season/episode, year, resolution, codec, size, publication time, seeders, and source trust. Do not select by seed count alone.
5. After authorization, run `ingest`. Use `--candidate` for cached structured results and a URL for web/direct sources.
   - A TV file without a season/episode token requires explicit `--season`/`--episode`.
   - Split unsupported multi-episode files before processing.
6. Run `check` until `done` or `failed`. After `stop`, run `check` again.
7. Report the output/archive path, actual mode/profile/naming/target, file count, and unmet requirements.

## Natural-language examples

- “Download this YouTube or Bilibili video, organize it as a movie, use the default transcode mode, and archive it to my movie library.”
- “Download this playlist as TV season 1 starting at episode 1, preserve the containers, and do not archive it yet.”
- “Process this local episode folder, complete titles, NFO, poster, and fanart, then organize it for Plex.”
- “Search for authorized sources for this show and show me candidates before downloading.”
- “Process this with the defaults.” Use `defaultModes.tv|movie` without another mode question once the media type is known.

Run `./run.sh --help` or a command-specific `--help` for the CLI contract.

## Commands

```bash
# Diagnose and inspect configuration
./run.sh doctor
./run.sh profiles
./run.sh sources

# Save a reviewed reusable source to private config.json; store env-var names, never keys
./run.sh add-source public-site 'https://example.test/search?q={query}' --type web
./run.sh add-source prowlarr 'http://127.0.0.1:9696/1/api' --type torznab \
  --api-key-env PROWLARR_API_KEY

# Search and use a cached structured candidate
./run.sh search "Show Name S01" --source jackett --type tv
./run.sh ingest "Show Name" --candidate CANDIDATE_ID --type tv --year 2026 \
  --profile tv1080 --target tv-library --naming plex

# Use a final web-media URL
./run.sh ingest "Movie Name" "https://authorized.example/video" --type movie \
  --downloader yt-dlp --profile movie1080 --target movie-library

# Map an explicitly approved YouTube/Bilibili playlist to sequential TV episodes
./run.sh ingest "Course Name" "PLAYLIST_URL" --type tv --downloader yt-dlp \
  --playlist --season 1 --episode 1 --no-transcode

# Process locally without any target configuration or archive transfer
./run.sh ingest "Course Name" "VIDEO_URL" --type tv --downloader yt-dlp \
  --season 1 --episode 1 --no-archive

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

# Organize without transcoding; preserve the original container
./run.sh organize "Show Name" "/path/to/Show.S01E02.mkv" --type tv \
  --metadata "/path/to/metadata.json" --target tv-library

# Preview, inspect, and stop
./run.sh ingest "Show Name" "magnet:?xt=urn:btih:..." --type tv --dry-run
./run.sh check "Show Name (2026)"
./run.sh stop "Show Name (2026)"
```

`resume` and `download` alias `ingest`; `process` aliases `adopt`. `organize` skips profile transcoding but still uses profile naming and an optional default target. Long-running commands launch in the background by default; add `--foreground` while debugging.

## Source behavior

- `auto` selects local for regular local files/directories, aria2 for magnet/`.torrent`/clear direct-media URLs, and yt-dlp for other HTTP(S) pages.
- `add-source` writes only to private, Git-ignored `config.json`, rejects embedded credentials and duplicate names, and requires `--replace` for a confirmed replacement.
- Structured candidates expire after seven days. The agent sees review fields and `candidateId`; the private `0600` cache retains the real download URL.
- Put signed/tokenized URLs in a user-owned regular `0600` `--source-file` rather than a command argument.
- Playlists are disabled by default. Add `--playlist` only after the user explicitly requests the whole list.
- For TV playlists without recognizable episode tokens, assign episodes in playlist order starting from `--season` (default 1) and `--episode` (default 1). Confirm ordering first.
- `--no-archive` needs no target or NAS. Transcoding/organization, NFO, and artwork still run; the status `targetPath` identifies the retained workspace output.

## Metadata and Plex

Try TMDB through `TMDB_API_KEY`; TV may fall back to TVMaze. A metadata JSON may override or supplement title, original/sort title, year, premiere date, plot, tagline, content rating, rating, runtime, status, genres, countries, tags, studio, directors, writers, actors, external IDs, episode details, and artwork URLs/paths.

Supported root artwork names are `poster`, `fanart`, `banner`, and `clearlogo`. With no TMDB key, still generate minimal valid NFO. If `metadata.requireArtwork=true` but neither a key nor supplied poster exists, warn and downgrade artwork to optional.

Verified `metadata.title` controls canonical naming and task identity while status retains the requested title. Enable “Use local Assets” in Plex for local artwork and select Plex NFO Agent for NFO. Use the Plex preset for catalogued movies and TV. Prefer a separate Plex “Other Videos” library for unmatched clips or ordinary channel uploads rather than disguising them as film/TV.

Transcoding defaults to MP4 profiles and removes inherited global and chapter metadata. Normalized filenames, NFO, and artwork carry library metadata. `--no-transcode` promises byte-preserving media organization and therefore does not alter embedded metadata.

## Configuration

Run `cp config.example.json config.json && chmod 600 config.json`. The example uses `$HOME/MediaDownloader`, defines no archive targets, and works with `--no-archive`; NAS is not required.

- `searchSources`: Jackett, generic Torznab/Prowlarr, or web templates; `apiKeyEnv` names an environment variable.
- `profiles`: container, resolution, codec, CRF/bitrate, optional target, and naming. Default profiles produce MP4.
- `defaultProfiles.tv|movie`: default compression profiles.
- `defaultModes.tv|movie`: `transcode` or `organize`; omitted values remain backward-compatible as `transcode`.
- `targets`: optional local directory, external-drive, or NAS presets; keep `{}` for local-only use.
- `namingPresets`: TV/movie path templates; `plex` is the default.
- `metadata`: TMDB, TVMaze fallback, and artwork requirements.

Environment overrides include `MEDIA_DOWNLOADER_CONFIG`, `MEDIA_DOWNLOADER_BASE_DIR`, `MEDIA_DOWNLOADER_STATE_DIR`, `MEDIA_DOWNLOADER_TARGET_DIR`, and `MEDIA_DOWNLOADER_OFFLINE=1`.

`stateDir` stores task locks and per-task logs. Global `status.json` and `candidates.json` default to `.runtime/` and can be overridden with `MEDIA_DOWNLOADER_STATUS_FILE` / `MEDIA_DOWNLOADER_CANDIDATE_FILE`.

After code or configuration changes, run `./scripts/smoke-test.sh`.
