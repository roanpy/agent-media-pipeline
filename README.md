<div align="center">
  <h1>Agent Media Pipeline</h1>
  <p><strong>Let an AI agent find authorized media, then run a deterministic download-to-library pipeline.</strong></p>

  English · [简体中文](README.zh-CN.md)

  [![Status: Beta](https://img.shields.io/badge/status-beta-2563eb.svg)](#project-status)
  [![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](#requirements)
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
| Explicit processing choices | TV/movie defaults with per-task `--transcode`, `--no-transcode`, playlist, and archive overrides |
| Plex-ready output | Movie and TV naming, NFO, subtitles, poster, fanart, banner, and clear logo assets |
| Optional archival | Keep output in a controlled local workspace or validate and atomically copy it to a configured target |
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
./run.sh doctor
```

The example configuration uses `$HOME/MediaDownloader` and defines no archive targets. Process a local movie without archiving:

```bash
./run.sh adopt "Example Movie" "/path/to/movie.mkv" \
  --type movie --no-archive
```

Download an authorized YouTube or Bilibili playlist, organize it as a TV season, keep the original containers, and leave the result locally:

```bash
./run.sh ingest "Course Name" "PLAYLIST_URL" \
  --type tv --downloader yt-dlp --playlist \
  --season 1 --episode 1 --no-transcode --no-archive
```

Run `./run.sh --help`, `./run.sh ingest --help`, or invoke the [`agent-media-pipeline` Skill](SKILL.md) and ask the agent in natural language.

## Requirements

- macOS or Linux
- Python 3.10+
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

# Search configured structured sources; download URLs remain private
./run.sh search "Show Name S01" --source jackett --type tv

# Add a reusable public search template after reviewing it
./run.sh add-source public-site \
  'https://example.test/search?q={query}' --type web

# Add a private Torznab endpoint; only the environment-variable name is saved
./run.sh add-source prowlarr \
  'http://127.0.0.1:9696/1/api' --type torznab \
  --api-key-env PROWLARR_API_KEY

# Transcode to the default MP4 profile and archive to a configured target
./run.sh adopt "Show Name" "/path/to/Show.S01E01.mkv" \
  --type tv --transcode --target tv-library

# Preserve the original container and organize only
./run.sh organize "Movie Name" "/path/to/Movie.mkv" \
  --type movie --target movie-library
```

Transcoding removes inherited global and chapter metadata by default, then relies on normalized filenames, NFO, and local artwork. Organize/no-transcode mode preserves the media bytes and therefore does not alter embedded metadata.

## Safety and privacy

- Search results expose review fields and candidate IDs, not stored download URLs.
- Signed or tokenized URLs can be supplied through a user-owned `0600` file with `--source-file`.
- `config.json`, runtime state, logs, and caches are Git-ignored; private JSON files are written as `0600`.
- Existing different files are never overwritten. Matching files are verified before being accepted.
- Archive cleanup runs only after size, SHA-256, media, and target-identity checks succeed.
- `--reset-work` removes only a verified task-owned workspace and must be used deliberately.
- Newly discovered reusable websites are saved only after user confirmation and only to private `config.json`.

## Plex compatibility

The default naming preset follows Plex's recommended movie and season/episode layout. Local assets include `poster`, `fanart`, `banner`, and `clearlogo`; movie, show, and episode NFO files are generated for the Plex NFO Agent. The official NFO Agent requires Plex Media Server 1.43.1 or newer. Enable local assets in the Plex library settings when using local artwork.

Ordinary channel uploads, clips, and unmatched web videos may fit a separate Plex “Other Videos” library better than a movie or TV library. This project does not automatically disguise arbitrary web content as catalogued film or television.

## Project status

The source is **beta**. The core local-file, HTTP, yt-dlp, transcode, organize, metadata, optional-target, archive, resume, stop, and safety paths are covered by a dependency-free integration test. Real indexers, public websites, downloader versions, network mounts, and long-running stop races remain environment-dependent.

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
