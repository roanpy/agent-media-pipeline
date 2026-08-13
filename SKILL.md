---
name: media-downloader
description: 搜索用户有权使用的 Jackett 或网页媒体来源，审查候选后用 aria2/yt-dlp/本地文件获取，选择按 TV/Movie 预设转码或保留原容器只整理，再按 Plex 或自定义模板命名、生成 NFO 与图片并校验归档。用于外部 AI Agent 完成媒体检索、下载、转码或免转码整理、归档、状态检查和安全停止。
---

# Media Downloader

把 Agent 判断与确定性执行分开：Agent 搜索、核实、选择；`run.sh` 下载、转码、整理、校验、归档和清理。

## 强制边界

- 仅处理用户有权访问、下载和归档的来源。不得绕过 DRM、付费墙、登录限制、验证码或站点条款。
- 不向用户提供侵权资源、破解方式、密钥或绕过措施。
- 搜索先返回候选；除非用户已给出精确来源或明确授权自动选择，否则在开始下载前展示候选依据并取得选择。
- 不在命令行、配置、日志或回复中暴露 API key、cookie、token 或带凭据的完整 URL。密钥只放环境变量。
- 目标预设目录必须已存在并可写；外置卷必须已挂载。不得创建伪挂载目录。
- 目标同名文件内容不同时拒绝覆盖。失败时保留来源和带所有权标记的工作区。
- 归档全部通过大小、SHA-256 和媒体有效性校验后，才清理本 Skill 创建的工作区。
- `--reset-work` 会删除该任务的失败工作区；只有用户确认重建同标题任务时使用。

## 固定流程

1. 运行 `./run.sh doctor`，确认 FFmpeg/FFprobe、目标预设和所需下载器。未挂载的可选目标显示为 `unavailable`，仅在本次选用它时才需要处理。
2. 明确类型、标题、年份、目标、是否转码、profile、命名 preset 与质量要求。类型或是否转码不确定时询问用户。
3. 搜索：
   - Jackett：`./run.sh search "查询" --source jackett --type tv|movie`
   - 网页：同一命令会返回 `browseUrl`；使用浏览器查看公开页面，提取用户有权使用的最终 URL。
   - Agent 可直接提供本地路径、magnet、`.torrent`、HTTP(S) 直链或网页媒体 URL。
4. 比较候选标题、季/集、年份、分辨率、编码、大小、发布时间、做种数和来源可信度。不要仅按做种数盲选。
5. 用户已授权选择后执行 `ingest`；Jackett 候选使用 `--candidate`，网页/直链使用 URL。
   - TV 文件名没有季集号时必须显式传 `--season`/`--episode`；多集单文件应先拆成单集。
6. 运行 `check` 跟踪到 `done` 或 `failed`。`stop` 后再次运行 `check`。
7. 报告归档路径、实际 profile/naming/target、文件数和任何未满足项。

## 命令

```bash
# 诊断和配置
./run.sh doctor
./run.sh profiles

# 搜索候选；输出 JSON，不含真实下载 URL
./run.sh search "剧名 S01" --source jackett --type tv
./run.sh search "片名 2026" --source web --type movie

# 使用缓存的 Jackett 候选
./run.sh ingest "剧名" --candidate CANDIDATE_ID --type tv --year 2026 \
  --profile tv1080 --target tv-library --naming plex

# 使用最终 URL；auto 自动选择 aria2 或 yt-dlp，也可显式指定
./run.sh ingest "片名" "https://authorized.example/video" --type movie \
  --downloader yt-dlp --profile movie1080 --target movie-library

# 带签名/token 的 URL 必须放进当前用户拥有、组/其他无权限的普通文件，避免出现在进程参数
chmod 600 /private/tmp/source-url
./run.sh ingest "片名" --source-file /private/tmp/source-url --type movie

# 手动下载的单集：文件名含 SxxExx 时自动识别，也可显式传 --season/--episode
./run.sh adopt "剧名" "/path/to/files" --type tv --year 2026 \
  --metadata "/path/to/metadata.json"

# 已归档同一集但要修正剧集/单集 NFO；只允许更新 .nfo
./run.sh adopt "剧名" "/path/to/Show.S01E02.mkv" --type tv --year 2026 \
  --metadata "/path/to/metadata.json" --update-nfo

# 只整理、不转码；保留原容器，仍按 Plex 命名并生成 NFO/图片
./run.sh organize "剧名" "/path/to/Show.S01E02.mkv" --type tv --year 2026 \
  --metadata "/path/to/metadata.json" --target tv-library
./run.sh organize "片名" "/path/to/Movie.mkv" --type movie --year 2026 \
  --metadata "/path/to/metadata.json" --target movie-library

# 预演、状态和停止
./run.sh ingest "剧名" "magnet:?xt=urn:btih:..." --type tv --dry-run
./run.sh check "剧名 (2026)"
./run.sh stop "剧名 (2026)"
```

`resume`/`download` 是 `ingest` 别名；`process` 是 `adopt` 别名。`organize` 只整理原文件，不按 profile 转码，但仍使用 profile 的默认 target/naming。耗时命令默认后台启动；调试时加 `--foreground`。

## 来源选择

- `auto`：本地普通文件/目录走 local；magnet、`.torrent`、明确媒体直链走 aria2；其他 HTTP(S) 页面走 yt-dlp。
- Jackett 结果缓存 7 天，仅向 Agent展示 `candidateId` 和审查字段；真实 URL 保存在权限为 `0600` 的本地缓存。
- 网页检索不在脚本里抓取或解析搜索结果。Agent 用浏览器核实公开页面，把最终授权 URL 交给 `ingest`。
- 带签名参数、cookie 或 token 的 URL 使用当前用户拥有、组/其他无权限的普通 `--source-file`；推荐 `chmod 600`，不要把它直接写在命令行。
- 播放列表默认关闭；用户明确要整个列表时才加 `--playlist`。

## 元数据

默认尝试 TMDB（环境变量 `TMDB_API_KEY`），TV 可回退 TVMaze。Agent 可用 `--metadata file.json` 覆盖或补充：

```json
{
  "title": "名称",
  "originalTitle": "Original Name",
  "year": 2026,
  "premiered": "2026-01-01",
  "plot": "简介",
  "genres": ["Drama"],
  "studio": "Studio",
  "ids": {"tmdb": 123, "imdb": "tt123"},
  "posterUrl": "https://authorized.example/poster.jpg",
  "fanartUrl": "https://authorized.example/fanart.jpg",
  "episodes": [
    {"season": 1, "episode": 1, "title": "第一集", "aired": "2026-01-01", "plot": "单集简介"}
  ]
}
```

图片也可用 `posterPath`/`fanartPath` 指向本地文件。未配置 TMDB 时仍会生成最小合法 NFO；`metadata.requireArtwork=true` 时缺海报会使任务失败。
`metadata.title` 是核实后的规范标题，会覆盖命令中的检索标题并用于目录、文件名和任务身份；状态中仍保留原检索标题。要让 Plex 读取 NFO，需使用支持 NFO 的 Plex Media Server 并为媒体库选择 Plex NFO Agent。
处理手动下载剧集时优先把文件名整理为 `Show.S01E02.ext`；无法改名时显式传 `--season 1 --episode 2`。已有 NFO 内容不同时默认拒绝覆盖；仅在核实新元数据后使用 `--update-nfo`，媒体、字幕和图片仍不会被覆盖。

## 配置

首次使用先执行 `cp config.example.json config.json`，再编辑不会被 Git 纳管的 `config.json`：

- `searchSources`：Jackett 或网页检索模板；Jackett key 由 `apiKeyEnv` 指向环境变量。
- `profiles`：容器、分辨率、codec、CRF/码率、默认 target 和 naming。
- `defaultProfiles.tv|movie`：TV/Movie 默认压缩 profile。
- `targets`：目标预设名和路径。
- `namingPresets`：TV/Movie 目录及文件模板；默认 `plex`。
- `metadata`：TMDB、TVMaze 回退和图片是否必需。

常用环境覆盖：`MEDIA_DOWNLOADER_CONFIG`、`MEDIA_DOWNLOADER_BASE_DIR`、`MEDIA_DOWNLOADER_STATE_DIR`、`MEDIA_DOWNLOADER_TARGET_DIR`、`MEDIA_DOWNLOADER_OFFLINE=1`。

修改代码或配置后运行：`./scripts/smoke-test.sh`。
