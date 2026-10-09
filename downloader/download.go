package downloader

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"golang.org/x/sync/errgroup"

	"github.com/HakuchumuHYX/novel-downloader-translator-webui/translator"
)

type Options struct {
	URL     string
	WorkDir string // 章节缓存在 <WorkDir>/chapters/<key>.txt，重跑时跳过已下载的章节
	OutDir  string
	Proxy   string
	Cookie  string
	Epub    bool
}

type chapterRef struct {
	key string
	url string
}

// Download 下载整本书，成品写到 <OutDir>/<书名>.txt 或 .epub，返回成品路径。
func Download(ctx context.Context, opts Options, logf translator.Logf, progress translator.Progress) (string, error) {
	site, err := DetectSite(opts.URL)
	if err != nil {
		return "", err
	}
	f, err := newFetcher(opts.Proxy, cookieHeader(opts.Cookie, site == "novel18"))
	if err != nil {
		return "", err
	}
	index, fetchChapter := syosetuIndex, syosetuChapter
	if site == "kakuyomu" {
		index, fetchChapter = kakuyomuIndex, kakuyomuChapter
	}

	chapterDir := filepath.Join(opts.WorkDir, "chapters")
	if err := os.MkdirAll(chapterDir, 0o755); err != nil {
		return "", err
	}
	title, chapters, err := index(ctx, f, opts.URL)
	if err != nil {
		return "", err
	}
	if len(chapters) == 0 {
		return "", fmt.Errorf("目录为空")
	}
	logf("《%s》共 %d 章", title, len(chapters))

	paths := make([]string, len(chapters))
	var pending []int
	for i, ch := range chapters {
		paths[i] = filepath.Join(chapterDir, ch.key+".txt")
		if info, err := os.Stat(paths[i]); err != nil || info.Size() == 0 {
			pending = append(pending, i)
		}
	}
	done := len(chapters) - len(pending)
	if done > 0 {
		logf("已缓存 %d 章，跳过", done)
	}
	progress(done, len(chapters))

	var mu sync.Mutex
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(8)
	for _, i := range pending {
		g.Go(func() error {
			if gctx.Err() != nil {
				return gctx.Err()
			}
			chapterTitle, content, err := fetchChapter(gctx, f, chapters[i].url)
			if err != nil {
				return err
			}
			tmp := strings.TrimSuffix(paths[i], ".txt") + ".tmp"
			if err := os.WriteFile(tmp, []byte("● "+chapterTitle+"\n"+content+"\n"), 0o644); err != nil {
				return err
			}
			if err := os.Rename(tmp, paths[i]); err != nil {
				return err
			}
			mu.Lock()
			defer mu.Unlock()
			done++
			progress(done, len(chapters))
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return "", err
	}

	parts := make([]string, len(paths))
	for i, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		parts[i] = strings.TrimSpace(string(data))
	}
	text := strings.Join(parts, "\n\n") + "\n"

	if err := os.MkdirAll(opts.OutDir, 0o755); err != nil {
		return "", err
	}
	name := SanitizeFilename(title)
	var result string
	if opts.Epub {
		result = filepath.Join(opts.OutDir, name+".epub")
		err = translator.WriteEpub(text, result, title, "ja")
	} else {
		result = filepath.Join(opts.OutDir, name+".txt")
		err = os.WriteFile(result, []byte(text), 0o644)
	}
	if err != nil {
		return "", err
	}
	logf("下载完成：%s", filepath.Base(result))
	return result, nil
}
