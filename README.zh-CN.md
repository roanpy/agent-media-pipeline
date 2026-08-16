<div align="center">
  <h1>Agent Media Pipeline</h1>
  <p><strong>让 AI Agent 搜索授权媒体，再由确定性流水线完成下载到媒体库的全过程。</strong></p>

  [English](README.md) · 简体中文

  [![状态：Beta](https://img.shields.io/badge/status-beta-2563eb.svg)](#项目状态)
  [![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg?logo=python&logoColor=white)](#运行要求)
  [![Agent Skill](https://img.shields.io/badge/Agent%20Skill-compatible-111827.svg)](SKILL.md)
  [![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
</div>

## 它解决什么问题

常见媒体自动化通常分成两类：简单的下载命令包装，或者需要长期运行的完整媒体服务器栈。Agent Media Pipeline 位于两者之间：AI Agent 负责搜索、核实候选和询问决策；本地 CLI 负责下载、转码或免转码整理、Plex 元数据生成、校验和可选归档。

不需要 NAS、媒体服务器、Docker 或 `*Arr` 服务也能使用。归档目标可以是本地目录、外置磁盘或网络存储，并且完全可选。

## 核心能力

| 能力 | 主要用途 |
| --- | --- |
| Agent 辅助发现 | Jackett、通用 Torznab/Prowlarr、可复用网页搜索模板，以及浏览器辅助的公开来源发现 |
| 多种获取方式 | 本地文件、magnet、torrent 文件、HTTP(S) 链接，以及 YouTube/Bilibili 等 yt-dlp 支持的网站 |
| 明确的处理选择 | TV/电影默认模式，以及逐任务转码、免转码、播放列表和归档开关 |
| Plex 友好输出 | 电影/剧集命名、NFO、字幕、海报、背景图、横幅和透明 Logo |
| 本地交付或归档 | 成品可交付到 `downloadDir`，也可预检后增量合并/原子归档到已有媒体库目标 |
| 安全与恢复 | 私有配置、来源脱敏、任务锁、失败工作区恢复、防覆盖、SHA-256、停止和挂载检查 |

项目只处理用户有权访问和下载的来源，不绕过 DRM、付费墙、认证控制、验证码或站点限制。

## 特别之处

- 它是带确定性执行后端的 Agent Skill，不只是提示词，也不只是下载器包装。
- Agent 判断与文件系统变更分离：先审查候选，实际操作走严格校验。
- 从来源发现一直覆盖到 Plex 目录，不要求部署长期运行的媒体自动化栈。
- 本地使用是一等路径，NAS 和外置媒体库都不是必需项。
- Python 只使用标准库，外部工具作为明确的运行时调用。

## 快速开始

```bash
git clone https://github.com/roanpy/agent-media-pipeline.git
cd agent-media-pipeline
cp config.example.json config.json
chmod 600 config.json
./run.sh doctor
```

示例配置把本地成品交付到 `$HOME/MediaDownloader/Incoming`，不包含任何归档目标。只在本地处理电影；完成后的 Plex 文件夹会落到该目录，并清理任务缓存：

```bash
./run.sh adopt "示例电影" "/path/to/movie.mkv" \
  --type movie --no-archive
```

下载用户有权使用的 YouTube/Bilibili 播放列表，按电视剧整理，不转码也不归档：

```bash
./run.sh ingest "课程名称" "PLAYLIST_URL" \
  --type tv --downloader yt-dlp --playlist \
  --season 1 --episode 1 --no-transcode --no-archive
```

运行 `./run.sh --help`、`./run.sh ingest --help`，或直接调用 [`agent-media-pipeline` Skill](SKILL.md) 用自然语言描述任务。

## 运行要求

- macOS 或 Linux
- Python 3.9+
- FFmpeg 和 FFprobe
- 可选：aria2，用于种子和直链
- 可选：yt-dlp，用于网页视频和播放列表
- 可选：Jackett、Prowlarr 或其他 Torznab 服务
- 可选：TMDB API key，用于更完整的电影/剧集资料

macOS 示例：

```bash
brew install ffmpeg aria2 yt-dlp
```

密钥仅放在 `TMDB_API_KEY`、`JACKETT_API_KEY`、`PROWLARR_API_KEY` 等环境变量中，不要写进 URL 或提交文件。

## 常用流程

```bash
# 查看工具、目标、profile 和私有搜索源
./run.sh doctor
./run.sh profiles
./run.sh sources

# 搜索结构化来源；真实下载 URL 不对外显示
./run.sh search "剧名 S01" --source jackett --type tv --timeout 90

# 用户审查确认后添加公开网页搜索模板
./run.sh add-source public-site \
  'https://example.test/search?q={query}' --type web

# 添加私有 Torznab 端点，只保存环境变量名
./run.sh add-source prowlarr \
  'http://127.0.0.1:9696/1/api' --type torznab \
  --api-key-env PROWLARR_API_KEY

# 使用默认 MP4 profile 转码并归档
./run.sh adopt "剧名" "/path/to/Show.S01E01.mkv" \
  --type tv --transcode --target tv-library --merge

# 保留原容器，只整理和归档
./run.sh organize "电影名" "/path/to/Movie.mkv" \
  --type movie --target movie-library
```

转码默认清理继承的全局和章节内嵌元数据，再由规范文件名、NFO 和本地图片提供 Plex 资料。免转码整理保持媒体字节不变，因此不会修改内嵌元数据。

## 安全与隐私

- 搜索结果仅公开审查字段和候选 ID，不公开缓存的下载 URL。
- 带签名或 token 的 URL 通过当前用户拥有的 `0600` 文件配合 `--source-file` 传入。
- `config.json`、运行状态、日志和缓存都被 Git 忽略；私有 JSON 文件按 `0600` 写入。
- 已存在且内容不同的目标文件永不覆盖。
- 归档会先完成冲突预检，避免最后因图片冲突而留下“部分成功”。电视剧分集增量归档可用 `--merge` 保留已有节目级图片和 `tvshow.nfo`，不同视频仍拒绝写入。
- aria2 连续零流量达到 `btStopTimeoutSeconds`（默认 600 秒）后停止；是否换源仍由 Agent 与用户明确决定。
- 只有大小、SHA-256、媒体有效性和目标身份校验全部通过后才清理工作区；调试时可用 `--keep-work` 保留任务缓存。
- `--reset-work` 只删除验证过所有权的任务工作区，必须明确使用。
- Agent 发现的新网站只有在用户确认后才会保存，并且只写入私有 `config.json`。

## Plex 兼容性

默认命名 preset 遵循 Plex 推荐的电影目录和电视剧季/集结构。本地资源包括 `poster`、`fanart`、`banner` 和 `clearlogo`；同时生成电影、剧集和单集 NFO，供 Plex NFO Agent 使用。官方 NFO Agent 要求 Plex Media Server 1.43.1 或更高版本。使用本地图片时需在 Plex 媒体库设置中启用本地资源。

普通频道视频、短片或无法匹配影视数据库的网络内容，通常更适合独立的 Plex “Other Videos” 库。本项目不会自动把任意网页视频伪装成正式电影或电视剧。

## 项目状态

当前源码处于 **Beta**。本地文件、HTTP、yt-dlp、转码、免转码整理、元数据、增量归档、恢复、子进程停止和主要安全路径已由无第三方 Python 依赖的集成测试覆盖。真实索引器、公开网站、下载器版本和网络挂载仍取决于运行环境。

暂未内置 qBittorrent 和 Transmission 下载客户端。当前 aria2 覆盖种子/直链下载，通用 Torznab 已覆盖 Prowlarr 类型的来源发现。未来只有在具备隔离下载目录、认证处理、完成轮询和明确清理语义时才应加入常驻下载器适配器。

## 开发与验证

```bash
./scripts/smoke-test.sh
python3 -m py_compile media-downloader.py tests/test_pipeline.py
ruff check media-downloader.py tests/test_pipeline.py
python3 /path/to/skill-creator/scripts/quick_validate.py .
```

## AI 开发协助说明

项目由所有者主导，并使用 AI 协助设计、实现、测试、审查和文档维护。项目所有者负责复核改动，并承担维护、安全和许可证决策责任。

## 许可证

项目源码使用 [MIT License](LICENSE)。FFmpeg、aria2、yt-dlp、Plex、TMDB、Jackett、Prowlarr 及各支持网站是独立项目或服务，适用各自许可证和条款。
