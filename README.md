# translator_webui

私用的小说翻译服务：粘贴链接下载小说（或上传 txt / epub）→ 调 OpenAI 兼容接口翻译 → 下载译文。

## 结构

```text
app/          FastAPI + Jinja 页面、SQLite、单线程任务队列
downloader/   python -m downloader —— 抓 syosetu / novel18 / kakuyomu，合并成一个 txt（可转 epub）
translator/   python -m translator —— 翻译 txt / epub
data/         运行时数据（app.sqlite3、tasks/<id>/{input,work,out}）
```

webui 以子进程方式调用 downloader 和 translator，停止任务就是杀掉子进程组。子进程通过 stdout 上报：

```text
__PROGRESS__ {"stage": "download|translate", "current": n, "total": m}
__RESULT__ "产物路径"
```

## 行为要点

- **续跑**：下载的章节缓存在 `work/chapters/`，译文按原文内容哈希缓存在 `work/translation_cache.json`。失败或停止后点“重试”只补缺的部分；试译完点“转全文”，已译的块不会重复请求。
- **切块**：txt 按 `● 章节标记` 和 80 行 / 12000 字切块；提示词需要要求模型逐行对应、保留 `●` 行，否则分章会乱。
- **上传 epub**：按 zip 直接改正文 xhtml 的 `p`、`h1`–`h6`，图片、目录、元数据原样保留。每批约 40 段编号送翻，编号对不上就退回逐段翻译。
- **下载任务选 epub**：先翻译 txt，再生成 epub。
- **novel18** 必须在设置页填登录后的 Cookie（会自动带上 `over18=yes`）。
- 请求不带 temperature。

## 部署

```bash
cp .env.example .env   # 改 Basic Auth 用户名和密码
docker compose up -d --build
```

端口只绑 `127.0.0.1`，对外访问请用反向代理（nginx 等）转发。API 地址、Key、模型、提示词、Cookie 等都在页面的“设置”里填，存在 `data/app.sqlite3`。

本地调试：

```bash
pip install -r requirements.txt
WEBUI_BASIC_AUTH_USER=u WEBUI_BASIC_AUTH_PASSWORD=p uvicorn app.main:app --port 7860
```

## 来源

爬虫解析逻辑源自 [syosetu_novel_downloader](https://github.com/ShiinaRinne/syosetu_novel_downloader)（MIT，© 2023 ShiinaRinne），txt 切块与续传思路源自 [bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker)（MIT，© 2023 yihong）。
