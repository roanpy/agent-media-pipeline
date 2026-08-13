---
name: media-downloader
description: 搜索用户有权使用的 Jackett、Torznab/Prowlarr 或网页媒体来源，审查候选后用 aria2/yt-dlp/本地文件获取，选择按 TV/Movie 预设转码或保留原容器只整理，再按 Plex 或自定义模板命名、生成 NFO 与图片并选择是否归档。用于外部 AI Agent 处理种子、直链、YouTube/Bilibili 单视频或播放列表、手动媒体、状态检查和安全停止。
---

# Media Downloader

把 Agent 判断与确定性执行分开：Agent 搜索、核实、选择；`run.sh` 下载、转码、整理、校验、归档和清理。

## 强制边界

- 仅处理用户有权访问、下载和归档的来源。不得绕过 DRM、付费墙、登录限制、验证码或站点条款。
- 不向用户提供侵权资源、破解方式、密钥或绕过措施。
- 搜索先返回候选；除非用户已给出精确来源或明确授权自动选择，否则在开始下载前展示候选依据并取得选择。
- 不在命令行、配置、日志或回复中暴露 API key、cookie、token 或带凭据的完整 URL。密钥只放环境变量。
- 归档是可选步骤。选择归档时，目标可为本地目录、外置磁盘或 NAS，必须已存在并可写；外置卷必须已挂载。不得创建伪挂载目录。
- 目标同名文件内容不同时拒绝覆盖。失败时保留来源和带所有权标记的工作区。
- 归档全部通过大小、SHA-256 和媒体有效性校验后，才清理本 Skill 创建的工作区。
- `--reset-work` 会删除该任务的失败工作区；只有用户确认重建同标题任务时使用。

## 固定流程

1. 运行 `./run.sh doctor`，确认 FFmpeg/FFprobe 和所需下载器。归档目标不是必需项；未挂载的可选目标显示为 `unavailable`，仅在本次选用它时才需要处理。
2. 明确类型、标题、年份、是否归档、目标、是否转码、profile、命名 preset 与质量要求。类型、是否转码或是否归档不确定时询问用户。
   - 先运行 `profiles` 查看 `defaultModes.tv|movie`。用户明确说“按默认”时直接使用；只给文件但未表达是否转码时，告知对应默认值并询问确认。
3. 搜索：
   - 先运行 `./run.sh sources` 查看已配置来源。也可使用浏览器主动搜索公开页面；只访问用户有权使用且不需绕过限制的来源。
   - Jackett：`./run.sh search "查询" --source jackett --type tv|movie`
   - 网页：同一命令会返回 `browseUrl`；使用浏览器查看公开页面，提取用户有权使用的最终 URL。
   - Agent 可直接提供本地路径、magnet、`.torrent`、HTTP(S) 直链或网页媒体 URL。
   - 发现值得复用的网站时，先向用户展示名称、域名、类型、URL 模板和凭据需求；用户明确确认后才用 `add-source` 保存到 Git 忽略且权限为 `0600` 的私有 `config.json`。不得把站点或密钥静默写进 `SKILL.md`、`config.example.json` 或代码。
4. 比较候选标题、季/集、年份、分辨率、编码、大小、发布时间、做种数和来源可信度。不要仅按做种数盲选。
5. 用户已授权选择后执行 `ingest`；Jackett 候选使用 `--candidate`，网页/直链使用 URL。
   - TV 文件名没有季集号时必须显式传 `--season`/`--episode`；多集单文件应先拆成单集。
6. 运行 `check` 跟踪到 `done` 或 `failed`。`stop` 后再次运行 `check`。
7. 报告归档路径、实际 profile/naming/target、文件数和任何未满足项。

## 可以直接这样问

- “帮我下载这个 YouTube/Bilibili 视频，按电影整理，使用默认转码并转移到 movie-library。”
- “帮我下载这个播放列表，按电视剧第 1 季从第 1 集开始整理，不转码，先不转移。”
- “帮我处理这个本地剧集目录，补齐标题、NFO、海报和背景图，按 Plex 格式归档。”
- “帮我搜索这部剧的授权来源，先给我候选，不要直接下载。”
- “按默认处理这个文件。”此时直接使用 `defaultModes.tv|movie`；如果只说“处理”且未表明类型/是否转码/是否转移，先询问关键选择。

底层命令帮助：`./run.sh --help`；具体命令帮助：`./run.sh ingest --help`、`./run.sh adopt --help`、`./run.sh organize --help`。

## 命令

```bash
# 诊断和配置
./run.sh doctor
./run.sh profiles
./run.sh sources

# 用户确认后保存公开网页搜索模板或 Torznab/Prowlarr 端点；只存环境变量名，不存 key
./run.sh add-source public-site 'https://example.test/search?q={query}' --type web
./run.sh add-source prowlarr 'http://127.0.0.1:9696/1/api' --type torznab \
  --api-key-env PROWLARR_API_KEY

# 搜索候选；输出 JSON，不含真实下载 URL
./run.sh search "剧名 S01" --source jackett --type tv
./run.sh search "片名 2026" --source web --type movie

# 使用缓存的 Jackett 候选
./run.sh ingest "剧名" --candidate CANDIDATE_ID --type tv --year 2026 \
  --profile tv1080 --target tv-library --naming plex

# 使用最终 URL；auto 自动选择 aria2 或 yt-dlp，也可显式指定
./run.sh ingest "片名" "https://authorized.example/video" --type movie \
  --downloader yt-dlp --profile movie1080 --target movie-library

# YouTube/Bilibili 播放列表按 TV 顺序整理；显式允许整个列表
./run.sh ingest "课程名" "https://www.youtube.com/playlist?list=..." --type tv \
  --downloader yt-dlp --playlist --season 1 --episode 1 --no-transcode

# 只下载/处理/生成 Plex 资料，不转移到磁盘或 NAS；结果留在输出的 targetPath
./run.sh ingest "课程名" "https://www.bilibili.com/video/..." --type tv \
  --downloader yt-dlp --season 1 --episode 1 --no-archive

# 带签名/token 的 URL 必须放进当前用户拥有、组/其他无权限的普通文件，避免出现在进程参数
chmod 600 /private/tmp/source-url
./run.sh ingest "片名" --source-file /private/tmp/source-url --type movie

# 手动下载的单集：文件名含 SxxExx 时自动识别，也可显式传 --season/--episode
./run.sh adopt "剧名" "/path/to/files" --type tv --year 2026 \
  --metadata "/path/to/metadata.json"

# 覆盖 TV/Movie 默认模式
./run.sh adopt "剧名" "/path/to/files" --type tv --transcode
./run.sh adopt "片名" "/path/to/Movie.mkv" --type movie --no-transcode

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
- Agent 可主动使用网页搜索发现一次性来源；新增长期来源属于配置写入，必须先获用户确认。`add-source` 只接受无内嵌凭据的 HTTP(S) URL，默认拒绝覆盖同名来源，确认替换时显式加 `--replace`。
- Jackett 结果缓存 7 天，仅向 Agent展示 `candidateId` 和审查字段；真实 URL 保存在权限为 `0600` 的本地缓存。
- 网页检索不在脚本里抓取或解析搜索结果。Agent 用浏览器核实公开页面，把最终授权 URL 交给 `ingest`。
- 带签名参数、cookie 或 token 的 URL 使用当前用户拥有、组/其他无权限的普通 `--source-file`；推荐 `chmod 600`，不要把它直接写在命令行。
- 播放列表默认关闭；用户明确要整个列表时才加 `--playlist`。
- YouTube/Bilibili 单视频及播放列表走 `yt-dlp`。TV 播放列表中无法从文件名识别季集号的项目按列表顺序编号，从 `--season`（默认 1）和 `--episode`（默认 1）开始；下载前应让用户确认顺序。
- `--no-archive` 不要求目标磁盘/NAS 可用，转码或免转码整理、NFO 和图片仍执行，成品保留在受控工作区 `output`；结果状态的 `targetPath` 给出路径。默认仍会归档转移。

## 元数据

默认尝试 TMDB（环境变量 `TMDB_API_KEY`），TV 可回退 TVMaze。Agent 可用 `--metadata file.json` 覆盖或补充：

```json
{
  "title": "名称",
  "originalTitle": "Original Name",
  "year": 2026,
  "premiered": "2026-01-01",
  "sortTitle": "排序标题",
  "plot": "简介",
  "tagline": "标语",
  "contentRating": "PG-13",
  "rating": 8.5,
  "runtime": 120,
  "status": "Ended",
  "genres": ["Drama"],
  "countries": ["中国"],
  "tags": ["课程"],
  "studio": "Studio",
  "directors": ["导演"],
  "writers": ["编剧"],
  "actors": [{"name": "演员", "role": "角色", "thumb": "https://authorized.example/person.jpg"}],
  "ids": {"tmdb": 123, "imdb": "tt123"},
  "posterUrl": "https://authorized.example/poster.jpg",
  "fanartUrl": "https://authorized.example/fanart.jpg",
  "bannerUrl": "https://authorized.example/banner.jpg",
  "clearlogoUrl": "https://authorized.example/logo.png",
  "episodes": [
    {"season": 1, "episode": 1, "title": "第一集", "aired": "2026-01-01", "plot": "单集简介"}
  ]
}
```

图片也可用对应的 `posterPath`/`fanartPath`/`bannerPath`/`clearlogoPath` 指向本地文件。未配置 TMDB 时仍会生成最小合法 NFO；`metadata.requireArtwork=true` 时缺海报会使任务失败，但既未配置 TMDB key、metadata 又未提供海报时自动降级为海报可选（会有 stderr 警告）。
`metadata.title` 是核实后的规范标题，会覆盖命令中的检索标题并用于目录、文件名和任务身份；状态中仍保留原检索标题。Plex 使用本地图片时在库设置启用 “Use local Assets”；使用 NFO 时选择 Plex NFO Agent。电影和正规剧集默认用 Plex 格式；无法匹配 TMDB/TVDB 的杂项网络视频更适合单独的 Plex “Other Videos” 库，本项目暂不将其伪装成影视条目。
处理手动下载剧集时优先把文件名整理为 `Show.S01E02.ext`；无法改名时显式传 `--season 1 --episode 2`。已有 NFO 内容不同时默认拒绝覆盖；仅在核实新元数据后使用 `--update-nfo`，媒体、字幕和图片仍不会被覆盖。

## 配置

首次使用先执行 `cp config.example.json config.json && chmod 600 config.json`。示例默认只用 `$HOME/MediaDownloader` 工作区、没有任何归档目标，因此不需要 NAS；直接加 `--no-archive` 即可下载、转码/整理并保留本地成品。需要归档时再在不会被 Git 纳管的 `config.json` 中添加本地目录、外置磁盘或 NAS target：

- `searchSources`：Jackett、通用 Torznab（含 Prowlarr）或网页检索模板；API key 由 `apiKeyEnv` 指向环境变量。
- `profiles`：容器、分辨率、codec、CRF/码率、可选默认 target 和 naming；默认 profile 优先输出 MP4。
- `defaultProfiles.tv|movie`：TV/Movie 默认压缩 profile。
- `defaultModes.tv|movie`：默认处理模式，填 `transcode` 或 `organize`；未配置时兼容为 `transcode`。
- `targets`：可选归档目标预设名和路径，可以是本地目录、外置磁盘或 NAS；只用 `--no-archive` 时保持 `{}`。
- `namingPresets`：TV/Movie 目录及文件模板；默认 `plex`。
- `metadata`：TMDB、TVMaze 回退和图片是否必需。

常用环境覆盖：`MEDIA_DOWNLOADER_CONFIG`、`MEDIA_DOWNLOADER_BASE_DIR`、`MEDIA_DOWNLOADER_STATE_DIR`、`MEDIA_DOWNLOADER_TARGET_DIR`、`MEDIA_DOWNLOADER_OFFLINE=1`。

状态分两处，别混淆：`stateDir`（或 `MEDIA_DOWNLOADER_STATE_DIR`）放任务工作区、任务锁和每任务日志；全局 `status.json` 与 `candidates.json` 默认在技能目录 `.runtime/`（可用 `MEDIA_DOWNLOADER_STATUS_FILE` / `MEDIA_DOWNLOADER_CANDIDATE_FILE` 覆盖）。任务身份 = 类型 + 规范标题 + 目标库 + 来源：同一标题换源重下互不覆盖，重放同一来源则复用同一任务。

转码默认清除来源文件的全局和章节内嵌元数据，避免残留原始标题、下载站备注或编码标记；Plex 信息由规范文件名、NFO 和本地图片提供。`--no-transcode` 承诺原文件字节不变，因此不会清理内嵌元数据。

## 开源安装

本目录符合开放 Agent Skills 的 `SKILL.md` 结构并使用 MIT 许可证。克隆到支持 Skills 的 Agent 所扫描目录，复制私有配置，然后运行 `./run.sh doctor` 和 `./scripts/smoke-test.sh`。需要 Python 3.10+，Python 代码只用标准库；运行时按用途安装 FFmpeg/FFprobe，以及可选的 aria2、yt-dlp。不同 Agent 的 Skill 扫描路径不同，以对应产品文档为准。

修改代码或配置后运行：`./scripts/smoke-test.sh`。
