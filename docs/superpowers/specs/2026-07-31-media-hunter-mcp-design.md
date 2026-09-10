# media-hunter-mcp 设计文档

日期：2026-07-31
状态：已获用户批准

## 1. 目标

构建一个 MCP（Model Context Protocol）服务器，让 AI 代理能够通过统一工具在四个站点上搜索并下载图片、漫画（画廊）、视频等资源：

- **e621**（图片 + 视频）
- **rule34.xxx**（图片 + 视频）
- **E-Hentai / ExHentai**（画廊 / 漫画）
- **pixiv**（插画 / 漫画 / ugoira 动图）

用户持有 pixiv 账号（refresh token）和 E-Hentai 账号（Cookie，可访问 ExHentai）。
内容分级不做默认过滤，过滤由调用方通过参数显式控制。

## 2. 技术选型

- **语言 / 框架**：Python + FastMCP（stdio 传输）
- **包管理**：uv（项目内虚拟环境）
- **HTTP 客户端**：httpx（异步、原生代理支持）
- **pixiv API**：pixivpy3（成熟的 app-api 封装）
- **测试**：pytest + respx（httpx mock），除可选冒烟测试外不依赖真实网络

关键 API 事实（2026-07 核实）：

| 站点 | 接口 | 认证 | 注意 |
|------|------|------|------|
| e621 | `GET /posts.json?tags=…&limit=…&page=…` | 描述性 User-Agent 必填；Basic Auth 可选（提高限额） | 响应自带原图/视频直链 |
| rule34.xxx | `GET api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1` | **强制** `user_id` + `api_key`（免费注册后设置页获取） | 未配置凭证时工具应明确提示 |
| E-Hentai | `POST api.e-hentai.org/api.php`（gdata / gtoken）+ 页面解析 | ExHentai 需 Cookie（ipb_member_id 等） | 元数据 25 条/请求，4-5 连发后歇 ~5s；原图下载有每日配额（509） |
| pixiv | `app-api.pixiv.net`（经 pixivpy3） | OAuth refresh_token | 图片请求需 `Referer: https://pixiv.net` |

## 3. 架构

适配器模式 + 共享基础设施：

```
media-hunter-mcp/
├── pyproject.toml
├── config.example.toml
├── src/media_mcp/
│   ├── server.py          # FastMCP 入口，注册 6 个工具
│   ├── config.py          # 配置加载（TOML + 环境变量覆盖）
│   ├── models.py          # 统一数据模型 Post / DownloadResult
│   ├── network.py         # 网络层：代理 + 镜像 fallback 链 + 每站点限速器
│   ├── downloader.py      # 共享下载引擎：重试 / 断点文件名 / 文件组织 / sidecar
│   └── sites/
│       ├── base.py        # SiteAdapter 抽象接口
│       ├── e621.py
│       ├── rule34.py
│       ├── ehentai.py
│       └── pixiv.py
└── tests/
    ├── fixtures/          # 录制的 API 响应样本（JSON/HTML）
    ├── test_e621.py · test_rule34.py · test_ehentai.py · test_pixiv.py
    └── test_downloader.py
```

各适配器实现要点：

- **e621**：`/posts.json` 响应自带 `file.url`（原图/视频直链），解析直接
- **rule34**：dapi JSON 响应解析；凭证缺失时 `search` 返回带注册引导的认证错误
- **E-Hentai**：`search` 用网页搜索页（`?f_search=…`）解析 HTML 得到 gid/token 列表，再用官方 `api.php` 的 `gdata` 方法批量补全元数据；`get_download_targets` 解析画廊页得到每页 `/s/{page_token}/{gid}-{page}` 图片页 URL，下载时逐页解析原图链接，感知 509 配额错误
- **pixiv**：经 pixivpy3 调用 `search_illust` / `illust_detail`；多图作品展开为多个下载目标；ugoira 下载官方 zip 后用本机 ffmpeg 合成

### 3.1 SiteAdapter 接口

每个站点适配器实现统一接口：

- `search(query, limit, page, min_score=None, rating=None) -> list[Post]`
- `get_post(id) -> Post`
- `get_download_targets(post) -> list[DownloadTarget]`（单文件返回 1 项，画廊返回 N 项）

适配器只负责"站点语义 ↔ 统一模型"的转换；网络访问一律经过 network.py，落盘一律经过 downloader.py。

### 3.2 统一数据模型 Post

```
site         str        # "e621" | "rule34" | "ehentai" | "pixiv"
id           str
url          str        # 来源页面 URL
title        str
tags         list[str]
artist       list[str]
rating       str        # s/q/e、e-hentai 分类、pixiv R-18 标记等原样保留
score        int | None
media_type   str        # image | video | gallery | ugoira
file_url     str | None # 单文件直链
preview_url  str | None
page_count   int        # 画廊页数，单文件为 1
extra        dict       # 站点特有字段（gid/token、illust_id 等）
```

工具返回统一信封：`{success: bool, data: … | error: {type, message, hint}}`。

## 4. 网络层（代理 + 镜像 fallback）

配置驱动，镜像域名**不硬编码**（镜像失效快，由用户自行维护配置）：

```toml
[network]
proxy = "http://127.0.0.1:7897"   # 梯子端口；留空 = 直连优先
timeout = 30
retries = 3

[sites.e621]
auth = { username = "", api_key = "" }   # 可选

[sites.rule34]
user_id = ""
api_key = ""

[sites.ehentai]
cookie = ""            # ipb_member_id=…; ipb_pass_hash=…; igneous=…
use_exhentai = true
# mirror_base = "https://e-hentai.org"   # 可替换为反代镜像

[sites.pixiv]
refresh_token = ""
# api_base = "https://app-api.pixiv.net" # 可替换为反代
# image_mirror = "https://i.pixiv.re"    # 图片 CDN 反代（备用）
```

**fallback 链**：每个请求依次尝试 直连（若未配代理）→ 配置代理 → 配置的镜像 base URL；全部失败才报错。配置模板注释中说明常见镜像类型（pixiv 图片反代 i.pixiv.re 类、Cloudflare Workers 反代等），不给具体保证。

**限速**：每站点 token bucket。默认值：e621 2 req/s；rule34 1 req/s；E-Hentai 元数据连发 ≤5 后歇 5s、图片下载 2-5s/张（可配）；pixiv 1 req/s。

## 5. MCP 工具（6 个）

| 工具 | 参数 | 返回 |
|------|------|------|
| `search` | site, query, limit=20, page=1, min_score=None, rating=None | `list[Post]`（不下载） |
| `get_post` | site, id | 单个 Post 完整元数据 |
| `download_post` | site, id, subdir=None | 下载结果（文件清单 + 保存目录） |
| `download_search` | site, query, limit=10, min_score=None, rating=None, subdir=None | 搜索并批量下载 |
| `download_url` | url, subdir=None | 自动识别站点并下载 |
| `self_check` | （无） | 各站点凭证 / 代理 / 镜像连通性报告 |

所有下载工具同步完成（单批 ≤50 文件，超时内部控制），返回绝对路径清单。

## 6. 文件组织

`download_root` 可配（如 `D:\Downloads\mcp-media`），根目录下固定模式自动分类：

```
{root}/e621/{YYYY-MM-DD}/{id}_{artist}_{md5前8}.{ext}      + {id}.json sidecar
{root}/rule34/{YYYY-MM-DD}/{id}_{md5前8}.{ext}             + {id}.json
{root}/ehentai/{gid}_{安全标题}/{页码:03d}.{ext}            + gallery.json
{root}/pixiv/{作者}_{uid}/{illust_id}_p{n}.{ext}           + {illust_id}.json
```

sidecar JSON 含站点、ID、来源 URL、标签、作者、时间、文件清单，供整理去重。ugoira 用本机 ffmpeg 合成 mp4/gif。

## 7. 错误处理

- **网络错误**：自动按 fallback 链重试（指数退避，≤3 次）
- **认证错误**：不重试，提示具体凭证配置项（如"请在 config.toml 的 [sites.pixiv] 填 refresh_token"）
- **配额错误**（E-Hentai 509 / pixiv rate limit）：返回明确类型与建议（等待 / 换镜像 / 降低限速）
- **内容不存在**：404 → 明确错误，附来源 URL
- **未知错误**：完整错误信息写入返回，不吞异常

## 8. 测试

- 每个适配器：用录制的 API 响应样本测试解析与模型转换（HTTP 全部 respx mock）
- 下载引擎：mock HTTP，验证命名 / 目录组织 / sidecar / 重试
- 网络层：验证 fallback 链顺序与限速行为
- 集成冒烟测试（pytest 标记 `@pytest.mark.smoke`，默认跳过）：真实凭证连通性

## 9. 范围外（YAGNI）

- pixiv 排行榜 / 收藏操作、E-Hentai 种子下载、e621 上传等写操作
- 去重数据库 / 下载历史（sidecar 已够用）
- Web UI、进度推送
