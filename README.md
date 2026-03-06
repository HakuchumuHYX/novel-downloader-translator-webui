# translator_webui

一个用于**小说下载 + 翻译编排**的 Web 控制台（WebUI）项目。它把“下载器”和“翻译器”封装成可重复执行的**任务**：创建任务 → 后台执行 → 实时日志 → 下载/预览产物。

整合模块：

- `webui`：FastAPI + Jinja2 的任务编排与管理界面
- `syosetu_novel_downloader`：多站点小说下载器（syosetu / novel18 / kakuyomu）
- `bilingual_book_maker`：多模型翻译引擎（可输出译文或双语）

---

## 功能亮点

- **任务化流程**：创建任务 → 后台执行 → 查看日志/下载产物
- **来源支持**：
  - 本地上传文件（upload）
  - kakuyomu
  - syosetu
  - syosetu-r18（需 Cookie Profile）
- **任务模式**：
  - 仅下载（download only）
  - 下载并翻译（download + translate）
- **翻译模式**：
  - 预览翻译（`test_num` 段/段落）
  - 全量翻译（full）
- **输出模式**：
  - `translated_only`（仅译文）
  - `bilingual`（双语）
- **任务操作**：
  - 重试任务（retry）：基于同一 payload 新建任务再次执行
  - 全量重跑（run-full）：基于原任务的源文件直接全量翻译（尽量复用已下载的 source）
  - 取消/停止：取消任务队列、或对运行中的子进程发起 stop
- **产物管理**：
  - `source / translated / manifest / log / other` 分类登记
  - 文件下载
  - txt/epub 在线预览（支持对比预览）
- **安全能力**：
  - Basic Auth
  - Cookie Profile **加密存储**（Fernet，需 `WEBUI_SECRET_KEY`）
  - 可启用安全默认策略（禁用默认口令、强制 secret key）

---

## 项目结构

```text
translator_webui/
├── .env.example
├── .env                      # 本地部署配置
├── docker-compose.yml
├── Dockerfile.webui
├── data/                     # 运行时数据目录
│   ├── webui.sqlite3         # WebUI SQLite 数据库
│   ├── tasks/                # 每个任务的工作目录与产物
│   └── uploads/              # 上传文件暂存目录
├── webui/                    # FastAPI WebUI（页面、API、worker）
├── syosetu_novel_downloader/ # 下载器
└── bilingual_book_maker/     # 翻译器
```

---

## 快速开始（Docker 推荐）

### 1) 准备配置

在项目根目录执行：

```bash
cp .env.example .env
```

生成 Fernet key（用于加密 cookie/profile 等敏感信息）：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将输出填入 `.env` 的 `WEBUI_SECRET_KEY`，并至少修改：

- `WEBUI_BASIC_AUTH_USER`
- `WEBUI_BASIC_AUTH_PASSWORD`

> 生产环境建议：开启 `WEBUI_ENFORCE_SECURE_DEFAULTS=true`，以禁止默认账号密码，并强制要求有效的 `WEBUI_SECRET_KEY`。

---

### 2) 构建并启动

```bash
docker compose up -d --build
```

默认访问地址：

- `http://localhost:7860`

端口来自 `.env` 中的 `WEBUI_PORT`（compose 里映射为 `${WEBUI_PORT:-7860}:7860`）。

---

### 3) 查看运行状态与日志

```bash
docker compose ps
docker compose logs -f novel-webui
```

---

### 4) 健康检查

服务提供 `/healthz` 探针，compose 与 Dockerfile 均配置了 healthcheck。

---

### 5) 挂载权限说明（重要）

compose 中包含 `init-data-permissions` 初始化服务，会先对 `./data` 做权限修复，然后再启动主服务 `novel-webui`。  
默认主服务使用非 root 用户 `app`（推荐）。

若宿主机挂载权限极端导致仍写入失败，可临时在 `.env` 里改为：

```env
WEBUI_CONTAINER_USER=0:0
```

排障完成后建议改回 `app`。

---

## 使用指南（WebUI 操作）

> WebUI 中的“Settings”用于配置 downloader/translator 的运行参数。  
> 翻译 API Key 等敏感字段会写入数据库；如配置了 `WEBUI_SECRET_KEY`，敏感信息会以加密形式存储。

### 1) Settings：配置翻译模型与 Key

常见路径：

- 进入 Settings 页面
- 填写模型相关参数（model / api_base / temperature / batch_size 等）
- 填写对应提供商的 key（openai / claude / gemini / groq / …）
- 保存后，后续任务会自动使用这些默认设置（也可被单个任务的 overrides 覆盖）

### 2) Cookies：创建 Cookie Profile（用于 R18/需要登录的站点）

- 支持直接粘贴 `Cookie:` header
- 也支持上传浏览器导出的 cookie json（WebUI 可解析并推断站点）
- Cookie 会加密存储；任务执行时会临时写入下载目录下的 cookie 文件，用完即删除

### 3) New Task：创建任务

关键字段说明（不同版本 UI 文案可能略有差异，以页面为准）：

- **source_type**
  - `upload`：上传本地 txt/epub 等
  - `kakuyomu / syosetu / syosetu-r18`：站点下载（r18 通常需要 cookie profile）
- **mode**
  - `download_only`：只下载，不翻译
  - `download_and_translate`：下载后翻译
- **translate_mode**
  - `preview`：预览翻译（配合 `test_num`）
  - `full`：全量翻译
- **translation_output_mode**
  - `translated_only`：仅译文
  - `bilingual`：双语
- **settings_overrides**
  - 用于“该任务专属”覆盖 settings（例如临时换模型、调并发等）

创建后任务会进入队列，后台 worker 会自动拉取执行。

### 4) Task Detail：查看执行、下载产物、预览对比

任务详情页一般包含：

- **实时日志**：轮询展示 worker 写入的 log
- **Artifacts**：按 `source/translated/...` 分类的文件列表，可逐个下载
- **预览**：txt/epub 在线分页预览；可选择右侧 compare 文件做对比

#### retry vs run-full vs cancel vs stop

- **retry**：用相同 payload 新建一个任务再跑一次
- **run-full**：基于原任务的源文件，强制 `download_and_translate + full`，并尽量复用已下载的 source
- **cancel**：取消任务（通常针对未运行/排队中的任务）
- **stop**：对运行中的任务发起停止请求，worker 会 terminate 子进程并在必要时 kill

---

## 环境变量配置（`.env`）

> 下面是 WebUI 进程读取的关键配置。  
> 业务参数（翻译 key、模型参数等）主要在 WebUI Settings 页面写入数据库；任务可通过 overrides 做局部覆盖。

### A. 鉴权与安全

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `WEBUI_BASIC_AUTH_USER` | `admin` | Basic Auth 用户名 |
| `WEBUI_BASIC_AUTH_PASSWORD` | `change_me` | Basic Auth 密码 |
| `WEBUI_SECRET_KEY` | 空 | Fernet 密钥（用于加密敏感信息） |
| `WEBUI_REQUIRE_SECRET_KEY` | `true/false` | 为 true 时，缺失密钥会阻止启动 |
| `WEBUI_ENFORCE_SECURE_DEFAULTS` | `true/false` | 为 true 时，禁止默认账号密码，且要求有效密钥 |
| `WEBUI_ENV` | `dev` | `prod/production` 下默认更严格 |

### B. 数据路径

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `WEBUI_DATA_DIR` | `/data`（Docker）/ `./data`（本地） | 运行数据根目录 |
| `WEBUI_DB_PATH` | `${WEBUI_DATA_DIR}/webui.sqlite3` | SQLite 数据库文件 |
| `WEBUI_TASK_ROOT` | `${WEBUI_DATA_DIR}/tasks` | 任务目录 |
| `WEBUI_UPLOAD_ROOT` | `${WEBUI_DATA_DIR}/uploads` | 上传文件目录 |

### C. Worker 与任务执行控制

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `WEBUI_WORKER_INTERVAL` | `1.0` | Worker 轮询间隔（秒） |
| `WEBUI_CLEANUP_DAYS` | `14` | 自动清理任务保留天数 |
| `WEBUI_PROCESS_TIMEOUT` | `7200` | 子进程最大执行时长（秒） |
| `WEBUI_STOP_GRACE_SECONDS` | `8` | stop 时先 terminate 的等待秒数 |
| `WEBUI_TASK_LOG_MAX_LINES` | `2000` | 前端日志展示长度上限（配置项） |

### D. 入口命令覆盖（高级）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `DOWNLOADER_PYTHON` | `python` | 下载器 Python 命令 |
| `DOWNLOADER_ENTRY` | `/app/syosetu_novel_downloader/main.py` | 下载器入口脚本 |
| `TRANSLATOR_PYTHON` | `python` | 翻译器 Python 命令 |
| `TRANSLATOR_ENTRY` | `/app/bilingual_book_maker/make_book.py` | 翻译器入口脚本 |
| `WEBUI_CONTAINER_USER` | `app` | 容器运行用户（默认非 root） |

### E. 网络端口

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `WEBUI_PORT` | `7860` | 对外暴露端口 |

---

## 运行原理（简要）

1. WebUI 启动：
   - 校验安全配置
   - 初始化 DB（SQLite + 表结构/迁移）
   - 启动后台 `TaskWorker`
2. 用户创建任务：
   - 保存 task payload（可带 settings_overrides、cookie profile、模板）
3. Worker 执行任务：
   - 非 upload 来源先跑 downloader（子进程）
   - 再按 mode/translate_mode 运行 translator（子进程）
   - 持续写入 task_logs（页面轮询展示）
   - stop/timeout 时会 terminate/kill 子进程并标记错误码
4. 结束后登记 artifacts：
   - `source / translated / manifest / log / other` 分类
   - 页面提供下载与预览

---

## 常见问题（Troubleshooting）

### 1) data 目录权限/写入失败

优先确认：

- `./data` 在宿主机可写
- compose 的 `init-data-permissions` 是否成功运行

仍异常时，可临时设置：

```env
WEBUI_CONTAINER_USER=0:0
```

排障完成后建议改回 `app`。

### 2) 服务启动时报错：secret key 缺失/不合法

- `WEBUI_REQUIRE_SECRET_KEY=true`：未设置 `WEBUI_SECRET_KEY` 会直接阻止启动
- `WEBUI_ENFORCE_SECURE_DEFAULTS=true`：除了要求 secret key，还会禁止使用默认账号密码

### 3) “停止任务”不立即生效

stop 的实现是：WebUI 发起 stop 请求 → worker 轮询检测 → `terminate`，超出 grace 时间后会 `kill`。  
因此 stop 的响应速度会受 `WEBUI_WORKER_INTERVAL` 与进程状态影响。

### 4) 翻译完成但找不到 translated 产物

worker 会按文件命名约定尝试解析翻译输出（例如 `*_翻译.txt/.epub` 或历史兼容 `*_bilingual.*`）。  
如果翻译器输出文件名不符合约定，会导致“翻译结束但未找到输出文件”。

---

## 本地开发运行（不走 Docker）

```bash
cd webui
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 7860
```

同时确保：

- `bilingual_book_maker` 与 `syosetu_novel_downloader` 依赖已安装
- `WEBUI_DATA_DIR` 可写
- 入口变量（`DOWNLOADER_ENTRY` / `TRANSLATOR_ENTRY`）路径可用

---

## 致谢

本项目在设计与实现上参考了以下优秀开源项目，特此感谢：

- [yihong0618/bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker)
- [ShiinaRinne/syosetu_novel_downloader](https://github.com/ShiinaRinne/syosetu_novel_downloader)

感谢原作者与社区贡献者的开源工作。
