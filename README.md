# media-hunter-mcp

搜索并下载 **e621、rule34.xxx、E-Hentai / ExHentai、Pixiv** 的作品。支持图片、视频、多图画廊及 Pixiv ugoira；提供 MCP 工具和独立命令行。

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。首次获取项目：

```powershell
git clone https://github.com/baichenxw/media-hunter-mcp.git
cd media-hunter-mcp
uv sync --locked --extra animation
```

已有项目时直接进入项目目录。首次配置时执行下面的命令，仅在文件不存在时复制，避免覆盖已有凭证：

```powershell
if (-not (Test-Path -LiteralPath config.toml)) {
  Copy-Item -LiteralPath config.example.toml -Destination config.toml
}
```

编辑本地 `config.toml`，填写需要使用的站点凭证，并修改 `[network].proxy`：示例值 `http://127.0.0.1:7897` 只适用于本机该端口确有代理的情况；不使用代理时填写 `proxy = ""`。完成后检查：

```powershell
uv run media-hunter check
```

`check` 会检查全部四个站点，未配置凭证的站点可能失败并导致退出码为 1；请查看 JSON 中各站的 `ok` 和错误信息。某站检查失败不妨碍调用其他已配置站点。

`animation` 提供 ugoira 转 GIF/MP4 所需的 FFmpeg。查找顺序为 `[sites.pixiv].ffmpeg_path` → 系统 PATH 中的 `ffmpeg` → `imageio-ffmpeg` 提供的程序。已有系统 FFmpeg 时可仅运行 `uv sync --locked`；只保存原始动图包则设置 `ugoira_format = "zip"`。下载命令中显式添加 `--extra animation` 可确保可选依赖已安装，详见 [uv 可选依赖说明](https://docs.astral.sh/uv/concepts/projects/sync/#syncing-optional-dependencies)。

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

每站可设置 `request_interval`、`download_delay`、`download_concurrency`（1–16）。每次网络尝试都限速；`download_delay` 控制待下载文件开始处理的间隔，`download_concurrency` 控制同一作品内部的并发数。同一服务进程内，同站点的下载调用会排队，不同站点可以同时下载；排队时间计入总超时。并发数和重试次数必须填写整数，布尔开关使用 TOML 的 `true` / `false`。

媒体文件的连接失败、HTTP 可重试错误及传输中断共用 `[network].retries` 次尝试，不会因两层重试而相乘。API 请求仍按每个候选地址分别计数。

`mirror_base` 是用户自行配置的可信反向代理。e621/rule34 的 API 在连接失败、429 或可重试 5xx 后尝试镜像；E-Hentai 的镜像作为主地址，支持 `https://example.com/eh` 这样的路径前缀。Pixiv 可分别设置 API/OAuth 主地址和图片镜像。API 镜像不会自动套用到图片 CDN 或 OAuth 地址。认证请求可能经配置的反向代理发送，请只使用自己信任的地址。

Pixiv 令牌在内存中自动续期。刷新返回的新 refresh token 会在当前服务进程内使用；重新启动仍读取配置中的值。配置文件不自动改写。

## MCP 接入

以 [OpenCode](https://opencode.ai/docs/mcp-servers/) 为例，下面是完整 JSON 示例；已有配置时将 `media-hunter` 条目合并到原来的 `mcp` 对象中。两处 `C:/Projects/media-hunter-mcp` 都需要替换为自己的项目绝对路径：

```json
{
  "mcp": {
    "media-hunter": {
      "type": "local",
      "command": [
        "uv", "run", "--project",
        "C:/Projects/media-hunter-mcp",
        "--locked", "--extra", "animation", "media-mcp"
      ],
      "enabled": true,
      "environment": {
        "MEDIA_HUNTER_CONFIG": "C:/Projects/media-hunter-mcp/config.toml"
      }
    }
  }
}
```

其他 MCP 客户端使用相同的命令、参数和环境变量，配置结构依客户端而定。默认使用 stdio，标准输出只传输 MCP 协议。更新项目后重新启动 MCP 客户端或其服务进程。

也支持 `MEDIA_HUNTER_TRANSPORT=http`（Streamable HTTP，默认 `/mcp` 路径）；默认监听 `127.0.0.1:8787`。可用 `MEDIA_HUNTER_HOST`、`MEDIA_HUNTER_PORT` 覆盖。`sse` 仅保留给旧客户端，新接入使用 stdio 或 Streamable HTTP。HTTP 模式未配置身份验证，应在受信任的本机环境使用。

1.3.0 使用 FastMCP 4 / MCP Python SDK 2，支持 [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) 的 `server/discover`、按请求协商、`resultType` 及列表缓存字段，也兼容旧版 `initialize` 流程。传输和版本转换交给 SDK；业务代码不自行拼接协议消息。工具声明只读/写入行为、参数范围和输出 JSON Schema。工具执行失败使用 `isError=true`，同时保留 JSON 文本与 `structuredContent`，部分完成的文件清单不会丢失。列表缓存提示为 60 秒、private，不缓存下载调用结果。

下载过程提供排队、详情、解析、文件完成和合成阶段的进度。只有客户端请求进度通知时才发送；消息中的页数表示当前作品进度，协议数值是单调递增的工作事件计数，总量未知时不伪造百分比。是否展示进度取决于客户端。

## 工具

| 工具 | 功能 |
| --- | --- |
| `search` | 站点原生标签/关键词搜索，支持 `limit`、`page`、`min_score`、`rating` |
| `get_post` | 作品元数据；E-Hentai 的 ID 格式为 `gid/token` |
| `download_post` | 下载整部作品或整本画廊，不受批量 50 文件上限限制 |
| `download_search` | 搜索并下载，整批最多尝试 50 个文件；超额画廊整本跳过 |
| `download_url` | 严格识别四站作品页面 URL 后下载，不接受任意文件直链 |
| `self_check` | 并行检查凭证与 API/首页连通性，每站最多 45 秒；不保证所有媒体链接可下载 |

`site` 使用 `e621`、`rule34`、`ehentai` 或 `pixiv`；ExHentai 同样使用 `ehentai`，通过配置切换。

搜索 `page` 从 1 开始。`limit` 上限：e621 320、rule34 1000、E-Hentai 100、Pixiv 30；批量下载的 `limit` 还限制为最多 50 个作品。E-Hentai 使用实际的 Next 游标顺序翻页，最多 100 页；深页查询比第一页慢。返回的是站点当前页中符合条件的结果，过滤后可能少于 limit。

`rating` 语义：e621 为 s/q/e 或完整名称；rule34 为 safe/questionable/explicit；Pixiv 为 all（不限）、safe、r18、r18g，后面三种精确匹配；E-Hentai 为画廊分类，例如 Manga、Non-H。`min_score` 对 Pixiv 表示收藏数，对 E-Hentai 表示星级。

下载工具的 `timeout` 是覆盖排队、搜索/详情、图片页解析、传输与合成的总秒数。省略表示不限制总时长；网络操作仍使用 `[network].timeout`。

## 下载结果

- 文件流式写入随机 `.part` 临时文件，完成且长度检查通过后原子替换目标文件。失败或取消会清理本次未完成文件。
- 作品下的 JSON sidecar 保存元数据、文件清单、SHA-256 和失败页；ugoira 的帧顺序和延时也会保存。
- E-Hentai 每本画廊有独立目录，即使指定相同 `subdir` 也不会覆盖另一本的页码文件。
- E-Hentai 图片页在每张实际下载前解析；单页失败会记录并继续其他页。认证或配额错误会停止当前作品未开始的下载，也会停止批量任务的后续作品。
- 部分失败时返回 `success: false`、`error.type: partial_download`，同时在 `data` 返回已完成文件及错误。批量下载还返回 `skipped` 和 `stop_reason`。
- 超时后已完成文件保留；取消中的作品清单写入 sidecar，批量结果的 `total_files` 统计已返回的作品结果，不包含取消中作品的残余完成文件。
- 默认在当前输出目录中校验清单的作品身份、文件来源、大小和 SHA-256，复用校验通过的文件，只补下载缺失或损坏的文件。E-Hentai 复用已有页时也会跳过该页的图片地址解析。
- MCP 下载工具设置 `overwrite=true`，或命令行加 `--overwrite`，可以强制重新下载。现有文件仍只在新下载成功后被替换。没有哈希的旧版清单会重新下载一次，建立新版校验记录。
- `files` 包括新下载和复用文件，文件项的 `reused` 表示是否复用；`new_files`、`reused_files` 分别计数。批量的 `total_files` 包括两者；`attempted_files` 是进入处理流程的目标数，包含复用目标，仍受 50 文件预算约束。
- 取消或重试失败会保留先前清单的文件索引；索引不替代校验，下次复用仍要检查实际文件。续传以完整文件为单位，不保留中断文件的部分字节；跨日期目录、作者目录变更及不同 `subdir` 之间不自动查重。
- ugoira 的 MP4 输出保留毫秒级帧时长；GIF 按格式限制四舍五入到 10 毫秒、最短 10 毫秒。播放器对短 GIF 帧的显示可能另有限制；精确时序优先使用 MP4 或原始 ZIP。

目录布局：e621/rule34 按日期归档；Pixiv 按作者归档；E-Hentai 按画廊 ID 和标题归档。`subdir` 是清理后的分组名，不是任意路径。

## 命令行

```powershell
uv run media-hunter check
uv run media-hunter search e621 "landscape" --rating safe --limit 5
uv run media-hunter search pixiv "風景" --rating safe --limit 5
uv run media-hunter get pixiv "作品ID"
uv run --extra animation media-hunter download-url "作品页面URL" --timeout 300
uv run media-hunter download-search e621 "landscape" --rating safe --limit 3 --timeout 180
uv run --extra animation media-hunter download pixiv "作品ID" --overwrite
```

将示例中的 `作品ID` 替换为实际数字 ID，将 `作品页面URL` 替换为完整页面链接；E-Hentai 的 ID 使用 `"gid/token"` 格式。

指定配置：`uv run media-hunter --config "配置文件路径" check`。业务结果输出 JSON；操作失败、部分完成或任一站点检查失败时退出码为 1，启动/配置失败为 2。`--help` 和命令行参数解析错误输出普通文本；Ctrl+C 中断的退出码为 130。配置校验错误会指出字段，例如 `network.timeout`，不回显该字段中的凭证或其他值。

## 验证与维护

```powershell
uv run --extra animation pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试默认不访问外部站点，覆盖 OAuth 续期、站点解析、下载中断、共享重试预算、取消清理、哈希复用、站点队列、文件上限、镜像画廊分页、真实 ffmpeg 逐帧时序、新旧版 stdio MCP 子进程及 Streamable HTTP 请求。手动联网检查：

```powershell
uv run --extra animation python tests/live_smoke.py
```

联网检查会下载 e621/Pixiv 的 safe 小样、尝试 E-Hentai Non-H 小样；rule34 只检查搜索、详情与 CDN HEAD。报告和小样写入 `.validation/`，不写入日常下载根目录。

代码结构：`server.py` 处理 MCP 接入与协议结果，`cli.py` 提供命令行；`service.py` 编排业务；`sites/` 负责站点协议；`network.py` 管理请求；`downloader.py` 管理校验复用、落盘与合成；`progress.py` 隔离每次调用的进度回调。新增站点时实现 `SiteAdapter` 并注册到 `MediaService`。

## 许可证

本项目采用 [MIT License](LICENSE)。
