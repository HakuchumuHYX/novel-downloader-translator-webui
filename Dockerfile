FROM golang:1.26-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go .
COPY app app
COPY downloader downloader
COPY translator translator
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/translator_webui .

FROM alpine:3.24

ENV DATA_DIR=/data

# uid/gid 固定成旧镜像 app 用户的值，./data 的属主不用改
RUN addgroup -S -g 101 app \
    && adduser -S -D -H -u 100 -G app -h /app -s /sbin/nologin app

COPY --from=build /out/translator_webui /usr/local/bin/translator_webui

USER app
EXPOSE 7860
CMD ["translator_webui"]
