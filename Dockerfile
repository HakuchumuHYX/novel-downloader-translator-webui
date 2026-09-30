FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# uid/gid 固定成旧镜像 app 用户的值，./data 的属主不用改
RUN groupadd --system --gid 101 app \
    && useradd --system --uid 100 --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY app app
COPY downloader downloader
COPY translator translator

USER app
EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
