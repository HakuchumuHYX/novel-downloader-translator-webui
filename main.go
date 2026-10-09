// 私用的小说翻译服务：下载 syosetu / novel18 / kakuyomu 小说或上传 txt / epub，调 OpenAI 兼容接口翻译。
package main

import (
	"cmp"
	"context"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/HakuchumuHYX/novel-downloader-translator-webui/app"
)

func mustEnv(key string) string {
	value := os.Getenv(key)
	if value == "" {
		log.Fatalf("缺少环境变量 %s", key)
	}
	return value
}

func main() {
	user := mustEnv("WEBUI_BASIC_AUTH_USER")
	password := mustEnv("WEBUI_BASIC_AUTH_PASSWORD")
	dataDir, err := filepath.Abs(cmp.Or(os.Getenv("DATA_DIR"), "data"))
	if err != nil {
		log.Fatal(err)
	}
	addr := cmp.Or(os.Getenv("LISTEN_ADDR"), ":7860")

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := app.Run(ctx, dataDir, user, password, addr); err != nil {
		log.Fatal(err)
	}
}
