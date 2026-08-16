<div align="center">
  <h1>Agent Media Pipeline</h1>
  <p><strong>Let an AI agent find authorized media, then run a deterministic download-to-library pipeline.</strong></p>

  English · [简体中文](README.zh-CN.md)

  [![Status: Beta](https://img.shields.io/badge/status-beta-2563eb.svg)](#project-status)
  [![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg?logo=python&logoColor=white)](#requirements)
  [![Agent Skill](https://img.shields.io/badge/Agent%20Skill-compatible-111827.svg)](SKILL.md)
  [![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
</div>

## The problem it solves

Media automation is usually split between simple download wrappers and large always-on server stacks. Agent Media Pipeline fills the middle ground: an AI agent searches, reviews, and asks for decisions; a local CLI performs downloading, transcoding or lossless organization, Plex-compatible metadata generation, validation, and optional archival.

It works without a NAS, a media server, Docker, or an `*Arr` stack. Archive targets can be local folders, external drives, or network storage, and are entirely optional.

## Key capabilities

| Capability | What it provides |
| --- | --- |
| Agent-guided discovery | Jackett, generic Torznab/Prowlarr, reusable web search templates, and browser-assisted public-source discovery |
| Multiple acquisition paths | Local files, magnet links, torrent files, HTTP(S) links, and yt-dlp-supported pages including YouTube and Bilibili |
| Explicit processing choices | TV/movie defaults with per-task transcode, organize, playlist, source-quality, authenticated-session, and archive overrides |
| Plex-ready output | Movie and TV naming (including optional episode titles), per-show/per-episode NFO, subtitles, episode thumbnails, poster, fanart, banner, and clear logo assets |
| Safe existing-library repair | Preview one show's folder, then repair episode names and per-episode NFO from reliable metadata without clobbering conflicts |
| Local delivery or archival | Deliver finished Plex folders to `downloadDir`, or preflight and atomically merge/archive them into an existing library target |
| Safety and recovery | Private configuration, redacted sources, task locks, resumable workspaces, no-clobber archival, SHA-256 checks, safe stop, and mounted-volume checks |

This project only handles sources the user is authorized to access and download. It does not bypass DRM, paywalls, authentication controls, CAPTCHAs, or site restrictions.

## Why it is different

- It is an Agent Skill with an executable, deterministic backend—not only a prompt and not only a download wrapper.
- Agent judgment stays outside the destructive path: candidates are reviewed before acquisition, while filesystem changes follow strict validation rules.
- It covers the complete handoff from source discovery to a Plex-ready folder without requiring a permanent media automation stack.
- Local-only use is first-class. NAS and external-library targets are optional.
- The Python implementation uses only the standard library; external tools are invoked as explicit runtimes.

## Quick start

```bash
git clone https://github.com/roanpy/agent-media-pipeline.git
cd agent-media-pipeline
cp config.example.json config.json
chmod 600 config.json
./run.sh --version
./run.sh doctor
```

The example configuration delivers local output to `$HOME/MediaDownloader/Incoming` and defines no archive targets. Process a local movie without archiving; the finished Plex folder is delivered there and the task cache is removed:

```bash
./run.sh adopt "Example Movie" "/path/to/movie.mkv" \
  --type movie --no-archive
```

Download an authorized YouTube or Bilibili playlist, organize it as a TV season, keep the original containers, and leave the result locally:

```bash
./run.sh probe "PLAYLIST_URL" --playlist
./run.sh ingest "Course Name" "PLAYLIST_URL" \
  --type tv --downloader yt-dlp --playlist \
  --season 1 --episode 1 --no-transcode --no-archive
```

`probe URL` lists a single video's available formats. `probe URL --playlist` returns a redacted JSON summary of playlist count and order. Add `--cookies chrome` (or a private `0600` Netscape cookies.txt path) to both probe and ingest only when the user's authorized YouTube/Bilibili session is required.

Use one `ingest` command for the whole web pipeline. It downloads, transcodes or organizes, writes NFO/artwork, delivers or archives, and cleans its cache without a second `adopt` command. Add `--no-deliver` when the finished folder must stay in the owned workspace with no transfer or cleanup.

Run `./run.sh --help`, `./run.sh ingest --help`, or invoke the [`agent-media-pipeline` Skill](SKILL.md) and ask the agent in natural language.

## Requirements

- macOS or Linux
- Python 3.9+
- FFmpeg and FFprobe
- Optional: aria2 for torrents/direct links
- Optional: yt-dlp for supported web video sites and playlists
- Optional: Jackett, Prowlarr, or another Torznab endpoint for structured search
- Optional: TMDB API key for richer movie/TV metadata

Examples on macOS:

```bash
brew install ffmpeg aria2 yt-dlp
```

Secrets stay in environment variables such as `TMDB_API_KEY`, `JACKETT_API_KEY`, or `PROWLARR_API_KEY`. Do not put credentials in URLs or committed files.

## Common workflows

```bash
# Inspect tools, targets, profiles, and private search sources
./run.sh doctor
./run.sh profiles
./run.sh sources

# Inspect one video's source formats, or a playlist's count and order
./run.sh probe "VIDEO_URL"
./run.sh probe "PLAYLIST_URL" --playlist --cookies chrome

# Search configured structured sources; download URLs remain private
./run.sh search "Show Name S01" --source jackett --type tv --timeout 90

# Add a reusable public search template after reviewing it
./run.sh add-source public-site \
  'https://example.test/search?q={query}' --type web

# Add a private Torznab endpoint; only the environment-variable name is saved
./run.sh add-source prowlarr \
  'http://127.0.0.1:9696/1/api' --type torznab \
  --api-key-env PROWLARR_API_KEY

# Transcode to the default MP4 profile and archive to a configured target
./run.sh adopt "Show Name" "/path/to/Show.S01E01.mkv" \
  --type tv --transcode --target tv-library --merge

# Download provider subtitles with a web playlist when the source offers them
./run.sh ingest "Course Name" "PLAYLIST_URL" --type tv --downloader yt-dlp \
  --playlist --write-subs --sub-langs "zh-CN,en"

# Preserve the original container and organize only
./run.sh organize "Movie Name" "/path/to/Movie.mkv" \
  --type movie --target movie-library

# Preview one existing show, then apply the reviewed episode-name/NFO repair
./run.sh repair "Show Name" "/path/to/Show Name (2026)" --year 2026 --season 1 \
  --metadata "/path/to/metadata.json"
./run.sh repair "Show Name" "/path/to/Show Name (2026)" --year 2026 --season 1 \
  --metadata "/path/to/metadata.json" --update-nfo --apply
```

Transcoding removes inherited global and chapter metadata by default, then relies on normalized filenames, NFO, and local artwork. Organize/no-transcode mode preserves the media bytes and therefore does not alter embedded metadata.

The default transcode container comes from the profile that `defaultProfiles.tv|movie` selects (MP4 in the example and current deployment). A profile with `container: "mkv"` keeps the source's embedded subtitle streams unchanged during transcode; MP4 still drops embedded subtitles for compatibility, while same-stem external subtitle files survive both modes. The example includes `movie1080-mkv` and `tv1080-mkv`; existing private configs may not contain newly added example profiles, so run `profiles` first.

For yt-dlp sources, omit `--format` for the best available streams or set a ceiling such as `--format "bv*[height<=720]+ba/b[height<=720]"`. This controls source selection only. The default transcode profile determines the final MP4 output; `--no-transcode` preserves yt-dlp's downloaded codecs and container. Playlists and Bilibili multi-part collections require `--type tv --playlist` and are mapped in confirmed playlist order from `--season`/`--episode`.

`--write-subs` is valid only for yt-dlp sources. It downloads manual/auto external subtitle tracks, converts them to `srt`, and carries them through organize/transcode as same-stem sidecars; sources without subtitles are skipped. `--sub-langs` requires `--write-subs`, takes comma-separated codes (e.g. `zh-CN,en`), and defaults to `metadata.subtitleLanguages`, metadata language, then `zh-CN`. No third-party subtitle provider is contacted.

The default Plex TV template appends a verified per-episode title when available: `Show - S01E03 - Episode title.ext`. With no reliable title it keeps `Show - S01E03.ext`. The same title and episode-specific provider ID are written to the sibling episode NFO; `tvshow.nfo` remains show-level and does not embed the episode catalogue. TMDB, TVMaze fallback, Jackett, and Prowlarr are optional; missing credentials do not block direct/local/web ingest.

`repair` accepts exactly one existing show folder and emits a JSON preview by default; only explicit `--apply` changes files. It supports Season 0 and carries same-stem subtitles, episode images, and NFO with the media. Missing reliable titles preserve original paths, and `tvshow.nfo` is never changed. Every new path is copied and byte-verified before the old path is removed, so interruption can leave duplicates but not lose media. Multi-episode files such as `S01E01-E02` must still be split first.

## Safety and privacy

- Search results expose review fields and candidate IDs, not stored download URLs.
- `--version`, doctor, dry-run, and task status expose the pipeline version/config schema for stale-install detection.
- Signed or tokenized URLs can be supplied through a user-owned `0600` file with `--source-file`.
- `config.json`, runtime state, logs, and caches are Git-ignored; private JSON files are written as `0600`.
- Existing different files are never overwritten. Matching files are verified before being accepted.
- Existing-library repair rejects library/category roots, duplicate SxxEyy media, and existing destinations; it previews by default and leaves legacy empty directories alone.
- Archive conflict preflight prevents a late artwork conflict from leaving a task partially delivered. For incremental TV episodes, `--merge` keeps existing different show-level artwork/`tvshow.nfo` while media conflicts still fail.
- aria2 stops after the configured sustained-zero-traffic interval (`btStopTimeoutSeconds`, default 600 seconds); source fallback remains an explicit agent/user decision.
- Delivery/archive cleanup runs only after size, SHA-256, media, and target-identity checks succeed. Use `--keep-work` to retain the task cache for debugging.
- `--reset-work` removes only a verified task-owned workspace and must be used deliberately.
- Newly discovered reusable websites are saved only after user confirmation and only to private `config.json`.

## Plex compatibility

The default naming preset follows Plex's recommended movie and season/episode layout. Local assets include `poster`, `fanart`, `banner`, and `clearlogo`; movie, show, and episode NFO files are generated for the Plex NFO Agent. The official NFO Agent requires Plex Media Server 1.43.1 or newer. Enable local assets in the Plex library settings when using local artwork.

Plex episode artwork uses the exact video basename with a `.jpg` extension: an agent-supplied `thumbPath`/`thumbUrl` takes precedence, then TMDB's episode `still_path`. Season artwork goes inside its season folder as `Season02.jpg`, or `season-specials-poster.jpg` for Season 0.

When adding S02 with `--merge`, existing show-level `tvshow.nfo`, poster, and fanart remain unchanged; the pipeline adds new episode NFO/artwork/subtitles and the season poster when available. Plex does not require rewriting show-level metadata for a new season, and `season.nfo` is optional. Legacy root `thumb.png` is not maintained as a Plex-standard asset.

Ordinary channel uploads, clips, and unmatched web videos may fit a separate Plex “Other Videos” library better than a movie or TV library. This project does not automatically disguise arbitrary web content as catalogued film or television.

## Project status

The source is **beta**. The core local-file, HTTP, yt-dlp, transcode, organize, metadata, incremental archive, resume, owned-child stop, and safety paths are covered by a dependency-free integration test. Real indexers, public websites, downloader versions, and network mounts remain environment-dependent.

qBittorrent and Transmission are not built-in download clients. aria2 handles the current torrent/direct-link path; generic Torznab already supports Prowlarr-compatible discovery. A future client adapter should only be added with isolated download directories, authenticated API handling, completion polling, and explicit cleanup semantics.

## Development and validation

```bash
./scripts/smoke-test.sh
python3 -m py_compile media-downloader.py tests/test_pipeline.py
ruff check media-downloader.py tests/test_pipeline.py
python3 /path/to/skill-creator/scripts/quick_validate.py .
```

## AI-assisted development disclosure

This project is owner-led and developed with AI assistance for design, implementation, testing, review, and documentation. The project owner reviews the resulting changes and remains responsible for maintenance, security, and licensing decisions.

## License

The project source is released under the [MIT License](LICENSE). FFmpeg, aria2, yt-dlp, Plex, TMDB, Jackett, Prowlarr, and supported websites are separate projects or services governed by their own licenses and terms.
