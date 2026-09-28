# media-hunter-mcp

搜索、预览并下载 **e621、rule34.xxx、E-Hentai / ExHentai、Pixiv** 的作品。支持图片、视频、多图画廊及 Pixiv ugoira；提供 MCP 工具和独立命令行。

## 快速开始

以下使用 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。首次获取项目：

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

### 使用 pip 安装

也可以使用 Python 3.11+ 自带的 pip，无需 uv。在克隆或解压后的项目目录中运行，以下为 Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[animation]"
```

按上文复制并编辑 `config.toml` 后，使用同一虚拟环境检查和启动：

```powershell
.\.venv\Scripts\media-hunter.exe --config "config.toml" check
.\.venv\Scripts\media-mcp.exe
```

Linux/macOS 将上述 `.\.venv\Scripts\` 替换为 `./.venv/bin/`，并去掉程序名的 `.exe`。如果已有系统 FFmpeg，或只保存 ugoira ZIP，可以将安装目标 `".[animation]"` 改为 `.`。

也可直接从 GitHub 的版本标签安装或升级（需要 Git；此命令替代上面的本地安装命令）：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade "media-hunter-mcp[animation] @ git+https://github.com/baichenxw/media-hunter-mcp.git@v1.3.3"
```

直接安装不会在当前目录生成配置模板，请从 [v1.3.3 的 config.example.toml](https://github.com/baichenxw/media-hunter-mcp/blob/v1.3.3/config.example.toml) 保存模板后配置。这里使用 GitHub 源码安装，不依赖同名 PyPI 包。pip 根据 `pyproject.toml` 解析依赖，不读取 `uv.lock`；需要按锁文件安装时使用上面的 uv 方式。语法参见 [pip 官方文档](https://pip.pypa.io/en/stable/topics/vcs-support/)。

接入 MCP 客户端时，将下方配置中的 `command` 改为仅含虚拟环境内 `media-mcp.exe` 绝对路径的数组，例如 `["C:/Projects/media-hunter-mcp/.venv/Scripts/media-mcp.exe"]`，并保留指向实际配置文件的 `MEDIA_HUNTER_CONFIG`。独立命令行示例则用该环境中的 `media-hunter` 替代 `uv run media-hunter`；安装时选择过 `[animation]` 后，无需再传 `--extra animation`。

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
| `[sites.ehentai]` | `cookie`；`use_exhentai = true`（默认）选里站，需要有效 Cookie；`allow_original = true`（默认）允许工具请求原图 |
| `[sites.pixiv]` | 必填 `refresh_token`；支持 `api_base`、`oauth_base`、`image_mirror` |

每站可设置 `request_interval`、`download_delay`、`download_concurrency`（1–16）。每次网络尝试都限速；`download_delay` 控制待下载文件开始处理的间隔，`download_concurrency` 控制同一作品内部的并发数。同一服务进程内，同站点的下载调用会排队，不同站点可以同时下载；排队时间计入总超时。并发数和重试次数必须填写整数，布尔开关使用 TOML 的 `true` / `false`。

媒体文件的连接失败、HTTP 可重试错误及传输中断共用 `[network].retries` 次尝试，不会因两层重试而相乘。API 请求仍按每个候选地址分别计数。

`mirror_base` 是用户自行配置的可信反向代理。e621/rule34 的 API 在连接失败、429 或可重试 5xx 后尝试镜像；E-Hentai 的 `mirror_base` 仅用于表站，`exhentai_mirror_base` 仅用于里站，分别作为所选站点的主地址，不跨站自动回退，支持 `https://example.com/eh` 这样的路径前缀。Pixiv 可分别设置 API/OAuth 主地址和图片镜像。API 镜像不会自动套用到图片 CDN 或 OAuth 地址。认证请求可能经配置的反向代理发送，请只使用自己信任的地址。

如果旧配置用 `mirror_base` 指向里站代理，请把该项改名为 `exhentai_mirror_base`；表站代理继续使用 `mirror_base`。

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

1.3.3 使用 FastMCP 4 / MCP Python SDK 2，支持 [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) 的 `server/discover`、按请求协商、`resultType` 及列表缓存字段，也兼容旧版 `initialize` 流程。传输和版本转换交给 SDK；业务代码不自行拼接协议消息。工具声明只读/写入行为、参数范围和输出 JSON Schema。工具执行失败使用 `isError=true`，同时保留 JSON 文本与 `structuredContent`，部分完成的文件清单不会丢失。列表缓存提示为 60 秒、private，不缓存下载调用结果。

下载过程提供排队、详情、解析、文件完成和合成阶段的进度。只有客户端请求进度通知时才发送；消息中的页数表示当前作品进度，协议数值是单调递增的工作事件计数，总量未知时不伪造百分比。是否展示进度取决于客户端。

## 工具

| 工具 | 使用场景与结果 |
| --- | --- |
| `media_hunter_search` | 只搜索；返回 `data.posts`（含 `site/id/url/title/page_count`）及本页 `count/page/limit`，不下载文件 |
| `media_hunter_get_post` | 读取已知 ID 的作品详情，返回 `data` 中的元数据，不下载文件 |
| `media_hunter_preview` | 看选定作品的实际缩略图；同站点 1–4 个 ID，返回 MCP 图片内容和 `data.previews` 对应清单，不保存媒体文件 |
| `media_hunter_download` | 只下载明确提供的同站点 `post_ids`，不搜索；输入 1–50 个 ID，去重后一个作品不限文件数，多个作品共用 50 文件预算；统一返回逐作品列表 |
| `media_hunter_download_url` | 已有作品页面链接时直接下载，自动识别站点和 ID；不接受搜索页面或媒体直链 |
| `media_hunter_self_check` | 检查凭证与 API/首页连通性，每站最多 45 秒；逐站查看 `data.<站点>.ok/detail`，不下载文件 |

例如先调用 `media_hunter_search`：

```json
{"site": "pixiv", "query": "風景", "rating": "safe", "limit": 5}
```

搜索到作品后可以直接展示结果；仅在需要保存媒体时调用下载工具。将返回结果的 `data.posts[].id` **原样作为字符串**放入 `media_hunter_download` 的 `post_ids` 数组，单个作品也使用数组：

```json
{"site": "pixiv", "post_ids": ["123"]}
```

多个作品改为 `"post_ids": ["123", "456"]`。已有 `data.posts[].url` 可直接交给 `media_hunter_download_url` 的 `url`，无需再搜索。

ID 必须来自同一站点；输入最多 50 项，重复 ID 按输入顺序去重，整批格式先校验再联网。**去重后一个 ID 下载整部作品，不限文件数；多个 ID 共用 50 个文件目标的预算，复用和失败目标也计入预算。** 超过剩余预算的作品整部跳过；模型可根据用户目标、页数及 `skipped` 原因决定拆分调用，大画廊单独用 `post_ids: [该 ID]` 下载。不同站点、表里站或原图设置需分次调用。这些决策规则也写在 MCP 工具及参数说明中。

无论单个还是多个，`data.requested_ids` 都是去重后的 ID 列表，`data.downloaded` 都是逐作品结果数组（每项含 `id`、`post`、`files` 等，也可能部分失败），`errors` 和 `skipped` 分别记录失败、未处理的 ID 及原因。下载不接受 `query`、`rating`、`limit` 或 `page`，这些条件只在搜索时使用。按链接下载仍直接返回 `data.files` 等单作品结果。

各工具通过参数 Schema 暴露说明、默认值和范围；下载结果的 `files[].path` 与 `sidecar_path` 均为运行服务器的本地路径。操作失败时 `success=false` / MCP `isError=true`；部分下载失败仍保留 `data` 中的文件清单。登录或额度错误先处理原因，其他失败可按原 ID 重试补齐，无需重新搜索。

### 先看预览图再筛选

搜索和详情只返回文字元数据，`preview_url` 是图片地址，不能等同于模型已看过图片。需要判断构图、颜色等画面内容时，将选定的 ID 交给预览工具：

```json
{"site": "pixiv", "post_ids": ["123", "456"]}
```

`media_hunter_preview` 按输入顺序去重，每个作品返回一张 JPEG：E 站为画廊封面，其他站为站点缩略图；多页作品只代表这一张，视频和动图仅提供静态预览。最多传 4 个 ID，更多作品分批处理；不同站点、表里站分次调用。E 站仍使用 `use_exhentai`，不提供 `original` 参数，也不会因预览失败而下载原图。

图片最长边不超过 640 像素，每张 JPEG 不超过 96 KiB；Base64 编码后每张最多约 128 KiB，4 张合计最多约 512 KiB，另有少量文字。压缩体积不等于模型视觉 token 用量，实际计费取决于提供商。原始缩略图限制为 8 MiB、2000 万像素；支持 JPEG、PNG、WebP、GIF 首帧，移除 EXIF 等元数据，透明区域使用白底。

返回遵循 [MCP 图片内容规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#image-content)：`content` 包含说明文字与 Base64 `ImageContent`，`structuredContent` 只放元数据，不重复塞入图片。`data.previews[].image_index` 从 1 开始，与实际图片顺序一致，`id` 用于后续下载；失败项没有图片。默认整批超时 45 秒，可通过 `timeout` 调整或设为 `null`；部分失败、超时仍保留已生成的图片，并在 `errors/skipped` 中说明未完成项。

**客户端必须将 MCP 图片块传给支持视觉输入的模型提供商。** 服务不需要保存模型 API Key，也不会自行选择另一家提供商；仅把图片 URL 或 JSON 文本交给模型的客户端无法完成看图。若模型没有收到图片，应明确说明，不能凭标签猜测。预览工具本身不写入下载目录，客户端可能为了显示、转发而缓存图片。

预览沿用 `[network].proxy`、限速和网络重试。图片请求仅访问该站点允许的图片域名，不向图片 CDN 发送账号 Cookie/API Token。Pixiv 沿用显式配置的 `image_mirror`，预览要求该镜像使用 HTTPS 默认端口，拒绝私网 IP 和本地域名；重定向也会检查来源。没有缩略图、图片不可访问或不符合限制时返回错误。只有需要保存作品时，才把筛选后的 ID 交给 `media_hunter_download`。

`site` 使用 `e621`、`rule34`、`ehentai` 或 `pixiv`；ExHentai 同样使用 `ehentai`，可通过每次调用的 `use_exhentai` 参数切换。

搜索 `page` 从 1 开始。`limit` 上限：e621 320、rule34 1000、E-Hentai 100、Pixiv 30。E-Hentai 使用实际的 Next 游标顺序翻页，最多 100 页；深页查询比第一页慢。返回的是站点当前页中符合条件的结果，过滤后可能少于 limit。

`rating` 语义：e621 为 s/q/e 或完整名称；rule34 为 safe/questionable/explicit；Pixiv 为 all（不限）、safe、r18、r18g，后面三种精确匹配；E-Hentai 为画廊分类，例如 Manga、Non-H。`min_score` 对 Pixiv 表示收藏数，对 E-Hentai 表示星级。

下载工具的 `timeout` 是覆盖排队、作品详情、图片页解析、传输与合成的总秒数。省略表示不限制总时长；网络操作仍使用 `[network].timeout`。

### E 站：选择表站、里站和原图

搜索、详情、预览、下载和检查工具均支持 `use_exhentai`：`true` 使用里站 ExHentai，`false` 使用表站 E-Hentai；省略时按配置决定，配置缺省及模板默认均为里站。`media_hunter_download_url` 是例外：省略时遵循链接域名，也可以显式覆盖。选择里站需要账号 Cookie，不会在失败时悄悄改用表站。旧配置中明确设置的 `use_exhentai = false` 仍然有效。

两个下载工具还支持 `original`：默认 `false` 下载页面图，设为 `true` 使用页面提供的原图入口。`allow_original = true` 只表示允许这一选择，不会让每次调用自动下载原图；设为 `false` 时原图请求会在联网前被拒绝。这两个工具参数仅适用于 E 站。

例如，模型可调用 `media_hunter_search` 搜索表站：

```json
{"site": "ehentai", "query": "landscape", "rating": "Non-H", "limit": 5, "use_exhentai": false}
```

调用 `media_hunter_download` 从里站下载指定画廊原图（将 `gid/token` 换成实际 ID）：

```json
{"site": "ehentai", "post_ids": ["gid/token"], "use_exhentai": true, "original": true, "timeout": 300}
```

原图下载可能消耗 FIQ（原图额度）或 GP，具体由账号权益、画廊时间和站点规则决定，参见 [E-Hentai 官方下载说明](https://ehwiki.org/wiki/Downloading)。登录或额度错误会停止后续下载；原图入口失败时不会自动退回缩放图。没有独立原图入口、且页面未标注缩放的图片，使用页面直接提供的源图。

页面图维持原目录；原图保存到该画廊的 `original/` 子目录，并保存独立清单。两种模式分别校验和复用，普通图不会被当作已完成的原图。结果中的 `post.extra.use_exhentai` 和 `post.extra.original` 表示本次选择。

## 下载结果

- 文件流式写入随机 `.part` 临时文件，完成且长度检查通过后原子替换目标文件。失败或取消会清理本次未完成文件。
- 作品下的 JSON sidecar 保存元数据、文件清单、SHA-256 和失败页；ugoira 的帧顺序和延时也会保存。
- E-Hentai 每本画廊有独立目录，即使指定相同 `subdir` 也不会覆盖另一本的页码文件。
- E-Hentai 图片页在每张实际下载前解析；单页失败会记录并继续其他页。认证或配额错误会停止当前作品未开始的下载，也会停止批量任务的后续作品。
- 部分失败时返回 `success: false`、`error.type: partial_download`，同时在 `data` 返回已完成文件及错误。按 ID 下载无论单个还是多个，还返回 `skipped` 和 `stop_reason`。
- `downloaded` 表示处理过的作品，不保证整部完成，须逐项查看 `complete/errors`。`skipped` 仅说明本次未处理，不能据此判断以前有没有下载文件；`total_files` 也不是磁盘文件总数。
- 超时后已完成文件保留；取消中的作品清单写入 sidecar，按 ID 下载结果的 `total_files` 统计已返回的作品结果，不包含取消中作品的残余完成文件。
- 默认在当前输出目录中校验清单的作品身份、文件来源、大小和 SHA-256，复用校验通过的文件，只补下载缺失或损坏的文件。E-Hentai 复用已有页时也会跳过该页的图片地址解析。
- MCP 下载工具设置 `overwrite=true`，或命令行加 `--overwrite`，可以强制重新下载。现有文件仍只在新下载成功后被替换。没有哈希的旧版清单会重新下载一次，建立新版校验记录。
- `files` 包括新下载和复用文件，文件项的 `reused` 表示是否复用；`new_files`、`reused_files` 分别计数。按 ID 下载的 `total_files` 包括两者；`attempted_files` 是进入处理流程的目标数，包含复用及失败目标，仅在去重后多个 ID 时受 50 文件预算约束。
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
uv run media-hunter download-posts e621 "作品ID1" "作品ID2" --timeout 180
uv run --extra animation media-hunter download pixiv "作品ID" --overwrite
uv run media-hunter search ehentai "landscape" --no-use-exhentai --limit 5
uv run media-hunter download ehentai "gid/token" --use-exhentai --original --timeout 300
```

将示例中的 `作品ID` 替换为实际数字 ID，将 `作品页面URL` 替换为完整页面链接；E-Hentai 的 ID 使用 `"gid/token"` 格式。

命令行保留 `download` 单作品快捷命令；`download-posts` 接受一个或多个 ID，文件预算及列表返回格式与 MCP `media_hunter_download` 一致。

指定配置：`uv run media-hunter --config "配置文件路径" check`。业务结果输出 JSON；操作失败、部分完成或任一站点检查失败时退出码为 1，启动/配置失败为 2。`--help` 和命令行参数解析错误输出普通文本；Ctrl+C 中断的退出码为 130。配置校验错误会指出字段，例如 `network.timeout`，不回显该字段中的凭证或其他值。

## 验证与维护

```powershell
uv run --extra animation pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试默认不访问外部站点，覆盖 OAuth 续期、站点解析、下载中断、搜索与下载隔离、明确 ID 批量处理、共享重试预算、取消清理、哈希复用、站点队列、文件上限、镜像画廊分页、表里站并发隔离、原图重定向与额度错误、原图独立复用、真实 ffmpeg 逐帧时序、新旧版 stdio MCP 子进程及 Streamable HTTP 请求。手动联网检查：

```powershell
uv run --extra animation python tests/live_smoke.py
```

联网检查会下载 e621/Pixiv 的 safe 小样、尝试 E-Hentai Non-H 小样；rule34 只检查搜索、详情与 CDN HEAD。报告和小样写入 `.validation/`，不写入日常下载根目录。

预览测试使用本地生成的图片，验证实际压缩、体积和像素限制、来源检查、重定向、Cookie 隔离、部分失败与超时保留，以及两种 MCP 协议模式下的图片块传输，不将测试图片写入下载目录。

代码结构：`server.py` 处理 MCP 接入与协议结果，`cli.py` 提供命令行；`service.py` 编排业务；`sites/` 负责站点协议；`network.py` 管理请求；`preview.py` 在内存中读取和压缩预览图；`downloader.py` 管理校验复用、落盘与合成；`progress.py` 隔离每次调用的进度回调。新增站点时实现 `SiteAdapter` 并注册到 `MediaService`。

## 许可证

本项目采用 [MIT License](LICENSE)。
