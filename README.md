# translator_webui

一个把“小说下载器 + 翻译器 + Web 控制台”打包到一起的私用项目。

它的目标很直接：

- 用 WebUI 创建任务
- 下载小说或上传本地文件
- 调用翻译引擎生成译文 / 双语输出
- 在页面里看日志、看进度、预览、下载产物

当前仓库由三部分组成：

- `webui`
  FastAPI + Jinja2 的任务编排、管理界面、后台 worker
- `syosetu_novel_downloader`
  小说下载器，负责站点抓取、规范化输出、manifest 生成
- `bilingual_book_maker`
  翻译引擎，负责 TXT / EPUB / PDF / Markdown / SRT 的翻译与输出

## 当前能力

- 支持任务化流程：创建、排队、运行、暂停、恢复、停止、重试、删除、清理
- 支持来源：
  - `upload`
  - `syosetu`
  - `syosetu-r18`
  - `kakuyomu`
- 支持任务模式：
  - `download_only`
  - `download_and_translate`
- 支持翻译模式：
  - `preview`
  - `full`
- 支持翻译输出：
  - `translated_only`
  - `bilingual`
- 支持上传格式：
  - `epub`
  - `txt`
  - `md`
  - `pdf`
  - `srt`
- 支持下载输出格式：
  - `txt`
  - `epub`
- 支持下载后端：
  - `auto`
  - `node`
  - `native`
- 支持翻译后端：
  - `openai`
  - `claude`
  - `gemini`
  - `groq`
  - `xai`
  - `qwen`
  - `deepl`
  - `deepl_free`
  - `caiyun`
  - `google`
  - `custom_api`
  - `tencent_transmart`
- 支持 Cookie Profile 管理与加密存储
- 支持 `.env` 导入 / 导出
- 支持系统状态页、健康检查、磁盘占用显示
- 支持日志流式查看和 txt / epub 在线预览

## 项目结构

```text
translator_webui/
├── .env.example
├── Dockerfile.webui
├── docker-compose.yml
├── README.md
├── data/                         # 运行时数据
│   ├── webui.sqlite3
│   ├── tasks/
│   └── uploads/
├── webui/                        # FastAPI WebUI
├── syosetu_novel_downloader/     # 下载器
└── bilingual_book_maker/         # 翻译器
```

运行时最重要的目录是：

- `data/webui.sqlite3`
  WebUI 的 SQLite 数据库
- `data/tasks/<task_id>/`
  每个任务自己的工作目录、下载结果、翻译结果、临时状态
- `data/uploads/`
  上传文件暂存目录

## 推荐部署方式

推荐直接用 Docker Compose。

### 1. 准备配置

```bash
cd /opt/translator_webui
cp .env.example .env
```

生成 Fernet Key：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

把结果填进 `.env`：

```env
WEBUI_SECRET_KEY=...
```

至少要改这些值：

- `WEBUI_BASIC_AUTH_USER`
- `WEBUI_BASIC_AUTH_PASSWORD`
- `WEBUI_SECRET_KEY`

`.env.example` 默认是偏安全的配置：

- `WEBUI_REQUIRE_SECRET_KEY=true`
- `WEBUI_ENFORCE_SECURE_DEFAULTS=true`

这意味着：

- 不能继续使用默认账号密码 `admin / change_me`
- 没有有效 `WEBUI_SECRET_KEY` 时服务会拒绝启动

### 2. 启动

```bash
docker compose up -d --build
```

默认访问地址：

```text
http://127.0.0.1:7860
```

端口来自：

```env
WEBUI_PORT=7860
```

### 3. 查看状态

```bash
docker compose ps
docker compose logs -f novel-webui
```

健康检查地址：

```text
/healthz
```

### 4. 数据目录权限

Compose 里包含一个 `init-data-permissions` 初始化服务，会先修正 `./data` 权限，再启动主容器。

正常情况下主容器以非 root 用户 `app` 运行。

如果你的宿主机挂载权限非常怪，导致 `/data` 仍不可写，可以临时在 `.env` 里改成：

```env
WEBUI_CONTAINER_USER=0:0
```

排障结束后建议改回：

```env
WEBUI_CONTAINER_USER=app
```

## 运行方式

WebUI 启动时会做这些事：

1. 读取环境配置并校验安全条件
2. 初始化 SQLite 表结构与必要修复
3. 回收异常退出后遗留的 running 任务状态
4. 启动后台 `TaskWorker`

任务执行时的链路是：

1. 用户在 WebUI 创建任务
2. 任务写入数据库，状态进入 `queued`
3. 后台 worker 领取任务并启动子进程
4. 如非 `upload` 来源，先运行 downloader
5. 如任务需要翻译，再运行 translator
6. 日志、进度、产物持续写回数据库和任务目录
7. 前端轮询 / SSE 拉取状态、日志、产物

## WebUI 页面

### 首页 `/`

- 展示任务列表
- 显示任务状态、创建时间、阶段、基础统计

### 新建任务 `/tasks/new`

用于创建任务。

主要字段：

- `source_type`
  - `upload`
  - `syosetu`
  - `syosetu-r18`
  - `kakuyomu`
- `mode`
  - `download_only`
  - `download_and_translate`
- `translate_mode`
  - `preview`
  - `full`
- `translation_output_mode`
  - `translated_only`
  - `bilingual`
- `backend`
  - `auto`
  - `node`
  - `native`
- `save_format`
  - `txt`
  - `epub`
- `cookie_profile_id`
  `syosetu-r18` 通常需要

页面还支持“任务级覆盖参数”。

这些覆盖值只作用于当前任务，不会改全局 Settings。

其中 `parallel_workers` 的全局默认值现在是：

```text
5
```

### 任务详情 `/tasks/{id}`

当前任务页支持：

- 实时日志
- 当前阶段与进度
- source / translated / manifest / log / other 产物列表
- 下载产物
- 在线预览
- 对比预览
- 重试
- run-full
- stop
- pause
- resume
- purge

### 批量管理 `/tasks/manage`

支持批量清理、批量删除。

### 设置 `/settings`

集中管理：

- 翻译模型参数
- API Key
- Prompt
- 下载参数
- Worker 参数
- Cookie Profile
- 模板
- `.env` 导入 / 导出

### 系统页 `/system`

展示：

- 当前路径配置
- Python / Node / npm 可用性
- 数据目录磁盘占用
- 安全相关状态

磁盘现在会显示为类似：

```text
12.4 GB / 100.0 GB (12.4%)
```

## 支持矩阵

### 下载来源

| 来源 | 说明 |
|---|---|
| `upload` | 上传本地文件，跳过下载阶段 |
| `syosetu` | 普通小説家になろう |
| `syosetu-r18` | ノクターン等 R18 站点，通常需要 Cookie |
| `kakuyomu` | カクヨム |

### 上传文件格式

| 格式 | 对应 loader |
|---|---|
| `epub` | `EPUBBookLoader` |
| `txt` | `TXTBookLoader` |
| `md` | `MarkdownBookLoader` |
| `pdf` | `PDFBookLoader` |
| `srt` | `SRTBookLoader` |

### 下载后端

| 后端 | 当前行为 |
|---|---|
| `auto` | 自动选择。`syosetu` 优先 `node`，`novel18` 优先 `native`，失败时按站点回退 |
| `node` | 使用 Node 侧抓取实现 |
| `native` | 使用 Python 原生抓取实现 |

当前 `auto` 的策略由下载器代码决定，不同站点的优先级不同。

### 翻译后端

当前注册的翻译模型键包括：

- `openai`
- `claude`
- `claude-sonnet-4-6`
- `claude-opus-4-6`
- `claude-opus-4-5-20251101`
- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-5-20250929`
- `claude-opus-4-1-20250805`
- `claude-opus-4-20250514`
- `claude-sonnet-4-20250514`
- `gemini`
- `groq`
- `xai`
- `qwen`
- `qwen-mt-turbo`
- `qwen-mt-plus`
- `deepl`
- `deepl_free`
- `caiyun`
- `google`
- `custom_api`
- `tencent_transmart`

其中 WebUI 默认翻译模型是：

```text
openai
```

## 配置项

项目里有两类配置来源：

1. 进程级环境变量
2. WebUI Settings 数据库存储项

### 进程级环境变量

这些由 `.env` 和 Docker Compose 提供。

#### 鉴权与安全

| 变量 | 说明 |
|---|---|
| `WEBUI_BASIC_AUTH_USER` | Basic Auth 用户名 |
| `WEBUI_BASIC_AUTH_PASSWORD` | Basic Auth 密码 |
| `WEBUI_SECRET_KEY` | Fernet 密钥，用于加密 Cookie / API Key 等敏感数据 |
| `WEBUI_REQUIRE_SECRET_KEY` | 为 `true` 时，没有有效密钥就拒绝启动 |
| `WEBUI_ENFORCE_SECURE_DEFAULTS` | 为 `true` 时禁止默认口令，并强制要求有效密钥 |
| `WEBUI_ENV` | 运行环境标识，如 `dev` / `prod` |

#### 数据与运行目录

| 变量 | 默认值 |
|---|---|
| `WEBUI_DATA_DIR` | `/data` |
| `WEBUI_DB_PATH` | `${WEBUI_DATA_DIR}/webui.sqlite3` |
| `WEBUI_TASK_ROOT` | `${WEBUI_DATA_DIR}/tasks` |
| `WEBUI_UPLOAD_ROOT` | `${WEBUI_DATA_DIR}/uploads` |

#### Worker / 任务控制

| 变量 | 默认值 |
|---|---|
| `WEBUI_WORKER_INTERVAL` | `1.0` |
| `WEBUI_CLEANUP_DAYS` | `14` |
| `WEBUI_PROCESS_TIMEOUT` | `7200` |
| `WEBUI_STOP_GRACE_SECONDS` | `8` |
| `WEBUI_TASK_LOG_MAX_LINES` | `2000` |
| `WEBUI_PROGRESS_MIN_INTERVAL_SECONDS` | `0.5` |

#### 入口覆盖

| 变量 | 默认值 |
|---|---|
| `DOWNLOADER_PYTHON` | `python` |
| `DOWNLOADER_ENTRY` | `/app/syosetu_novel_downloader/main.py` |
| `TRANSLATOR_PYTHON` | `python` |
| `TRANSLATOR_ENTRY` | `/app/bilingual_book_maker` |
| `WEBUI_CONTAINER_USER` | `app` |

### WebUI Settings 项

这部分由 `webui/app/option_registry.py` 注册并持久化到数据库。

主要包括：

- 模型相关
  - `model`
  - `model_list`
  - `api_base`
  - `language`
  - `source_lang`
  - `temperature`
  - `deployment_id`
- Prompt 相关
  - `prompt_file`
  - `prompt_text`
  - `prompt_system`
  - `prompt_user`
- 翻译行为
  - `test`
  - `test_num`
  - `resume`
  - `accumulated_num`
  - `parallel_workers`
  - `use_context`
  - `context_paragraph_limit`
  - `block_size`
  - `translation_style`
  - `batch_size`
  - `translate_tags`
  - `exclude_translate_tags`
  - `allow_navigable_strings`
  - `interval`
- 下载行为
  - `proxy`
  - `timeout`
  - `retries`
  - `rate_limit`
  - `backend`
  - `paid_policy`
  - `save_format`
  - `merge_all`
  - `merged_name`
  - `record_chapter_number`
- 清理 / 运行控制
  - `cleanup_days`
  - `cleanup_statuses`
  - `process_timeout`
- 敏感字段
  - `openai_key`
  - `claude_key`
  - `gemini_key`
  - `groq_key`
  - `xai_key`
  - `qwen_key`
  - `caiyun_key`
  - `deepl_key`
  - `custom_api`

这些敏感字段在配置了有效 `WEBUI_SECRET_KEY` 后会加密存储。

## Cookie Profile

Cookie 管理支持两种输入：

- 直接粘贴 `Cookie:` header
- 上传浏览器导出的 cookie JSON

WebUI 可以：

- 从 JSON 解析出 header
- 尝试推断站点
- 把 Cookie 保存为 Profile

Cookie Profile 主要用于：

- `syosetu-r18`
- 其他需要登录态的抓取场景

## 任务与产物

每个任务都会产生：

- 数据库记录
- 日志
- 任务目录
- 产物登记

产物会按类型分类：

- `source`
- `translated`
- `manifest`
- `log`
- `other`

`run-full` 的行为是：

- 把原任务强制转成 `download_and_translate + full`
- 如果已有可复用 source 文件，直接把它当 upload 源重跑
- 尽量跳过重复下载

`purge` 和 `delete` 的区别：

- `purge`
  清理任务输出，但保留任务记录
- `delete`
  删除任务记录，可选级联、可选删任务目录和上传文件

## API 概览

主要 API 路由包括：

- 页面
  - `GET /`
  - `GET /tasks/new`
  - `GET /tasks/manage`
  - `GET /tasks/{task_id}`
  - `GET /settings`
  - `GET /system`
- 任务
  - `POST /api/tasks`
  - `GET /api/tasks`
  - `GET /api/tasks/{task_id}`
  - `POST /api/tasks/{task_id}/retry`
  - `POST /api/tasks/{task_id}/run-full`
  - `POST /api/tasks/{task_id}/cancel`
  - `POST /api/tasks/{task_id}/stop`
  - `POST /api/tasks/{task_id}/pause`
  - `POST /api/tasks/{task_id}/resume`
  - `POST /api/tasks/{task_id}/purge`
  - `DELETE /api/tasks/{task_id}`
  - `POST /api/tasks/manage/batch-purge`
  - `POST /api/tasks/manage/batch-delete`
- 日志 / 产物 / 预览
  - `GET /api/tasks/{task_id}/logs`
  - `GET /api/tasks/{task_id}/logs/stream`
  - `GET /api/tasks/{task_id}/artifacts`
  - `GET /api/tasks/{task_id}/preview`
  - `GET /api/tasks/{task_id}/download`
- 设置
  - `POST /api/settings`
  - `POST /api/settings/import-env`
  - `GET /api/settings/export-env`
- Cookie
  - `POST /api/cookies/parse-json`
  - `POST /api/cookies`
  - `DELETE /api/cookies/{profile_id}`
- 系统
  - `GET /healthz`
  - `GET /api/system/status`

除 `GET /healthz` 和 `GET /redirect/settings` 之外，其余页面和 API 都需要 Basic Auth。

## 本地开发

如果你不想用 Docker，也可以直接本地运行 WebUI。

### 1. 安装依赖

```bash
cd /opt/translator_webui
python3 -m venv .venv
. .venv/bin/activate
pip install -r bilingual_book_maker/requirements.txt
pip install PyMuPDF
pip install -r syosetu_novel_downloader/requirements.txt
pip install -r webui/requirements.txt
pip install -r webui/requirements-dev.txt
```

Node 运行时也需要可用，因为 downloader 的 `node` 后端会用到。

### 2. 配置环境变量

`.env.example` 是给 Docker Compose 准备的，里面的 `/data`、`/app/...` 路径是容器内路径。

本地直接运行时，最稳妥的方式是先加载 `.env`，再覆盖成本机仓库路径：

```bash
cp .env.example .env
set -a
. ./.env
set +a
export WEBUI_DATA_DIR="$(pwd)/data"
export WEBUI_DB_PATH="$(pwd)/data/webui.sqlite3"
export WEBUI_TASK_ROOT="$(pwd)/data/tasks"
export WEBUI_UPLOAD_ROOT="$(pwd)/data/uploads"
export DOWNLOADER_ENTRY="$(pwd)/syosetu_novel_downloader/main.py"
export TRANSLATOR_ENTRY="$(pwd)/bilingual_book_maker"
```

### 3. 启动

```bash
uvicorn webui.app.main:app --host 0.0.0.0 --port 7860 --reload
```

## 常见问题

### 1. `syosetu-r18` 下载失败

先检查：

- 是否配置了 Cookie Profile
- Cookie 是否过期
- 站点选择是否正确

### 2. 翻译任务“秒完成”，但内容还是原文

这通常不是任务真的成功了，而是输入文件的正文结构没有被旧规则识别。

当前仓库已经对 EPUB 做了正文文本节点回退处理，避免因为没有 `<p>` 标签而整章静默跳过。

### 3. `TRANSLATOR_ENTRY` 指向旧脚本路径

当前有效入口应该是目录：

```text
/app/bilingual_book_maker
```

不是旧的：

```text
/app/bilingual_book_maker/make_book.py
```

### 4. `parallel_workers` 为什么默认是 5

这是当前项目层面的标准默认值：

```text
parallel_workers = 5
```

全局设置和任务级覆盖都已经按这个值对齐。

### 5. Docker 启动但 `/data` 无法写入

优先检查宿主机目录权限。

实在不行可以临时：

```env
WEBUI_CONTAINER_USER=0:0
```

但不建议长期这样跑。

## 许可证

仓库根目录采用 `LICENSE` 中的许可。

另外两个内嵌组件目录也各自保留了原始许可证文件：

- `bilingual_book_maker/LICENSE`
- `syosetu_novel_downloader/LICENSE`

如果你继续大改，建议把“你自己的 WebUI 部分”和“上游/移植过来的子模块”继续分开看待。
