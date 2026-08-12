---
name: media-downloader
description: 对用户明确提供的媒体来源执行下载或接管、转换、校验、清理，并转移到指定磁盘。
---

# Media Downloader

## 边界

- 只处理用户明确提供的合法来源或本地媒体目录，不检索来源。
- 主链只有：下载/接管 → 转换 → 校验 → 转移到指定磁盘 → 清理工作区。
- 不负责 NAS 自动挂载、Plex 刷新、海报、NFO 或媒体库刮削。
- 目标磁盘根目录必须已经存在；脚本不会创建缺失的根目录，以免磁盘未挂载时误写系统盘。
- 目标文件已存在且大小不同时拒绝覆盖。
- 只有目标文件完成大小和媒体有效性校验后，才清理本地转换目录、临时下载和运行状态。

## 配置

`config.json`：

- `baseDir`：下载及转换工作区。
- `targetDir`：最终目标磁盘目录。
- `stateDir`：Transmission 和任务运行状态目录。
- `profiles.*.targetDir`：不同媒体预设的目标目录。

环境变量优先：

- `MEDIA_DOWNLOADER_BASE_DIR`
- `MEDIA_DOWNLOADER_TARGET_DIR`
- `MEDIA_DOWNLOADER_STATE_DIR`

旧的 `MEDIA_DOWNLOADER_NAS_ROOT`、`nasRoot` 配置仍兼容，但不再自动挂载 NAS。

## 命令

```bash
./run.sh resume "名称" "magnet:..." [--profile=tv1080]
./run.sh adopt "名称" [/path/to/source] [--profile=tv1080]
./run.sh process "名称" [--profile=tv1080]
./run.sh check "名称"
./run.sh stop "名称"
./run.sh profile [预设名]
```

- `resume`：使用 Transmission 获取已提供来源，然后转换并转移。
- `adopt`：接管已有媒体文件，然后转换并转移。
- `process`：校验工作区内已有 MP4 并转移。
- `stop` 后应再执行一次 `check`。

当前下载后端只支持 `transmission-cli` 可接受的来源。扩展网页视频来源时，优先让新下载器输出到现有临时目录，再复用 `adopt` 后半段，不新建第二套转换和转移流程。
