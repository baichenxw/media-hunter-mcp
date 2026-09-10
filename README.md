# media-hunter-mcp

搜索并下载 **e621、rule34.xxx、E-Hentai / ExHentai、Pixiv** 的作品。支持图片、视频、多图画廊及 Pixiv ugoira；提供 MCP 工具和独立命令行。

## 快速开始

在项目目录运行（Python 3.11+、uv）：

```powershell
uv sync --extra animation
uv run media-hunter check
```

`animation` 安装项目内使用的 ffmpeg，可将 ugoira 转成 GIF/MP4。已有系统 ffmpeg 时可仅运行 `uv sync`，或设置 `ugoira_format = "zip"` 保存原始动图包。

首次安装时复制 `config.example.toml` 为 `config.toml`，填写站点凭证。已有 `config.toml` 可继续使用。

## 配置

读取顺序：`MEDIA_HUNTER_CONFIG` 环境变量 → 当前目录 `config.toml` → 源码项目目录 `config.toml` → `~/.config/media-hunter/config.toml`。命令行的 `--config` 优先于以上规则。相对下载目录以配置文件所在目录为基准。

| 配置 | 用途 |
| --- | --- |
| `download_root` | 下载根目录，支持 `~` 和相对路径 |
| `[network].proxy` | HTTP(S)/SOCKS5 代理；空字符串表示直连，不读取系统代理环境变量 |
| `[network].timeout` | 单次 HTTP 网络操作的超时秒数 |
| `[network].retries` | 每个候选地址的最大尝试次数，1–10，包含首次请求 |
| `[sites.e621]` | 可选 `username`、`api_key`；可自定义描述性 `user_agent` |
| `[sites.rule34]` | 必填 `user_id`、`api_key`，在站点账户 Options 页面生成 |
| `[sites.ehentai]` | `cookie`；`use_exhentai = true` 时必须提供 Cookie |
| `[sites.pixiv]` | 必填 `refresh_token`；支持 `api_base`、`oauth_base`、`image_mirror` |

每站可设置 `request_interval`、`download_delay`、`download_concurrency`（1–16）。每次网络尝试都限速；`download_delay` 控制各文件开始处理的间隔，`download_concurrency` 控制同一作品内部的并发数。同一服务进程的不同下载调用会排队，排队时间计入总超时。

`mirror_base` 是用户自行配置的可信反向代理。e621/rule34 的 API 在连接失败、429 或可重试 5xx 后尝试镜像；E-Hentai 的镜像作为主地址。Pixiv 可分别设置 API/OAuth 主地址和图片镜像。API 镜像不会自动套用到图片 CDN 或 OAuth 地址。认证请求可能经配置的反向代理发送，请只使用自己信任的地址。

Pixiv 令牌在内存中自动续期。刷新返回的新 refresh token 会在当前服务进程内使用；重新启动仍读取配置中的值。配置文件不自动改写。

## MCP 接入

以 OpenCode 为例，在其配置的 `mcp` 段添加：

```json
"media-hunter": {
  "type": "local",
  "command": [
    "uv", "run", "--project",
    "C:/Projects/media-hunter-mcp",
    "--extra", "animation", "media-mcp"
  ],
  "enabled": true,
  "environment": {
    "MEDIA_HUNTER_CONFIG": "C:/Projects/media-hunter-mcp/config.toml"
  }
}
```

其他 MCP 客户端使用相同的命令、参数和环境变量，配置结构依客户端而定。默认使用 stdio，标准输出只传输 MCP 协议。更新项目后重新启动 MCP 客户端或其服务进程。

也支持 `MEDIA_HUNTER_TRANSPORT=http`（Streamable HTTP，默认 `/mcp` 路径）或 `sse`；默认监听 `127.0.0.1:8787`。可用 `MEDIA_HUNTER_HOST`、`MEDIA_HUNTER_PORT` 覆盖。HTTP 模式未配置身份验证，应在受信任的本机环境使用。

## 工具

| 工具 | 功能 |
| --- | --- |
| `search` | 站点原生标签/关键词搜索，支持 `limit`、`page`、`min_score`、`rating` |
| `get_post` | 作品元数据；E-Hentai 的 ID 格式为 `gid/token` |
| `download_post` | 下载整部作品或整本画廊，不受批量 50 文件上限限制 |
| `download_search` | 搜索并下载，整批最多尝试 50 个文件；超额画廊整本跳过 |
| `download_url` | 严格识别四站作品页面 URL 后下载，不接受任意文件直链 |
| `self_check` | 并行检查凭证与 API/首页连通性，每站最多 45 秒；不保证所有媒体链接可下载 |

搜索 `page` 从 1 开始。`limit` 上限：e621 320、rule34 1000、E-Hentai 100、Pixiv 30；批量下载的 `limit` 还限制为最多 50 个作品。E-Hentai 使用实际的 Next 游标顺序翻页，最多 100 页；深页查询比第一页慢。返回的是站点当前页中符合条件的结果，过滤后可能少于 limit。

`rating` 语义：e621 为 s/q/e 或完整名称；rule34 为 safe/questionable/explicit；Pixiv 为 all（不限）、safe、r18、r18g，后面三种精确匹配；E-Hentai 为画廊分类，例如 Manga、Non-H。`min_score` 对 Pixiv 表示收藏数，对 E-Hentai 表示星级。

下载工具的 `timeout` 是覆盖排队、搜索/详情、图片页解析、传输与合成的总秒数。省略表示不限制总时长；网络操作仍使用 `[network].timeout`。

## 下载结果

- 文件流式写入随机 `.part` 临时文件，完成且长度检查通过后原子替换目标文件。失败或取消会清理本次未完成文件。
- 作品下的 JSON sidecar 保存元数据、文件清单和失败页；ugoira 的帧顺序和延时也会保存。
- E-Hentai 每本画廊有独立目录，即使指定相同 `subdir` 也不会覆盖另一本的页码文件。
- E-Hentai 图片页在每张实际下载前解析；单页失败会记录并继续其他页，认证或配额错误会停止尚未开始的请求。
- 部分失败时返回 `success: false`、`error.type: partial_download`，同时在 `data` 返回已完成文件及错误。批量下载还返回 `skipped`。
- 超时后已完成文件保留；取消中的作品清单写入 sidecar，批量结果的 `total_files` 统计已返回的作品结果，不包含取消中作品的残余完成文件。
- 重试会重新请求作品文件；现有完整文件只在新下载成功后被替换。

目录布局：e621/rule34 按日期归档；Pixiv 按作者归档；E-Hentai 按画廊 ID 和标题归档。`subdir` 是清理后的分组名，不是任意路径。

## 命令行

```powershell
uv run media-hunter check
uv run media-hunter search e621 "landscape" --rating safe --limit 5
uv run media-hunter search pixiv "風景" --rating safe --limit 5
uv run media-hunter get pixiv <作品ID>
uv run --extra animation media-hunter download-url "作品页面URL" --timeout 300
uv run media-hunter download-search e621 "landscape" --rating safe --limit 3 --timeout 180
```

指定配置：`uv run media-hunter --config "配置文件路径" check`。命令行输出 JSON；操作失败或部分完成退出码为 1，启动/配置失败为 2。

## 验证与维护

```powershell
uv run --extra animation pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试默认不联网，覆盖 OAuth 续期、站点解析、下载中断、取消清理、文件上限、画廊分页、真实 ffmpeg GIF/MP4 合成和真实 stdio MCP 子进程。手动联网检查：

```powershell
uv run --extra animation python tests/live_smoke.py
```

联网检查会下载 e621/Pixiv 的 safe 小样、尝试 E-Hentai Non-H 小样；rule34 只检查搜索、详情与 CDN HEAD。报告和小样写入 `.validation/`，不写入日常下载根目录。

代码结构：`server.py` 仅处理 MCP 接入，`cli.py` 提供命令行；`service.py` 编排业务；`sites/` 负责站点协议；`network.py` 管理请求；`downloader.py` 管理落盘与合成。新增站点时实现 `SiteAdapter` 并注册到 `MediaService`。

## 许可证

本项目采用 [MIT License](LICENSE)。

实际凭证只填写在本地 `config.toml` 中；该文件、部署资料、备份和验证产物均已加入 `.gitignore`。公开仓库仅提供凭证为空的 `config.example.toml`。
