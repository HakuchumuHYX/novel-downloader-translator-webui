# WebUI

FastAPI-based orchestration UI for:
- `syosetu_novel_downloader`
- `bilingual_book_maker`

## Features
- Settings page mapped to translation/downloader runtime options
- Task page with source type: upload / kakuyomu / syosetu / syosetu-r18
- Background queue worker with task status + live logs
- Artifact list with file-by-file download
- txt/epub preview pages
- Cookie profile encryption at rest
- Basic Auth access control

## Local run

```bash
cd webui
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 7860
```

## Env vars

- `WEBUI_BASIC_AUTH_USER`
- `WEBUI_BASIC_AUTH_PASSWORD`
- `WEBUI_SECRET_KEY` (Fernet key)
- `WEBUI_DATA_DIR` (default `../data`)
- `DOWNLOADER_PYTHON` / `DOWNLOADER_ENTRY`
- `TRANSLATOR_PYTHON` / `TRANSLATOR_ENTRY`
- `WEBUI_CONTAINER_USER` (docker-compose user override, default `app`)

## Docker 配置约定（避免重复配置）

- `docker-compose.yml` 使用 `env_file: .env` 作为运行时配置入口。
- `DOWNLOADER_ENTRY` / `TRANSLATOR_ENTRY` 建议只在 `.env` 中维护，不要在 compose 的 `environment` 重复声明。
- `WEBUI_CONTAINER_USER` 用于覆盖容器运行用户，默认 `app`（推荐保持非 root）。
- compose 内置 `init-data-permissions` 一次性初始化服务：会先对 `./data` 执行权限修复，再启动 `novel-webui`。
- 如遇极端环境仍有权限异常，可临时改为 `0:0` 排障，完成后建议改回 `app`。
