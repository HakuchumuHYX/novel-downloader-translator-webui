// Package translator 调 OpenAI 兼容接口翻译 txt / epub。
//
// 请求里不带 temperature：新模型大多忽略或直接拒绝这个参数。
package translator

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"golang.org/x/sync/errgroup"
)

type Config struct {
	APIBase      string
	APIKey       string
	Model        string
	Language     string
	PromptSystem string
	PromptUser   string
	Workers      int
	Proxy        string
}

type Options struct {
	Epub       bool // txt 输入时输出 epub；epub 输入总是输出 epub
	Bilingual  bool
	TestBlocks int // 只翻前 N 块，0 为全文
}

type Logf func(format string, args ...any)

type Progress func(current, total int)

// Translate 翻译 src，产物写到 outDir，返回产物路径。译文缓存在 <workDir>/translation_cache.json，重跑自动续传。
func Translate(ctx context.Context, cfg Config, src, workDir, outDir string, opts Options, logf Logf, progress Progress) (string, error) {
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return "", err
	}
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return "", err
	}
	t, err := newTranslator(cfg, filepath.Join(workDir, "translation_cache.json"), logf, progress)
	if err != nil {
		return "", err
	}
	logf("模型 %s，并发 %d", cfg.Model, cfg.Workers)

	var result string
	if strings.ToLower(filepath.Ext(src)) == ".epub" {
		result, err = translateEpub(ctx, src, outDir, t, opts.Bilingual, opts.TestBlocks)
	} else {
		result, err = translateTxt(ctx, src, outDir, t, opts)
	}
	if err != nil {
		return "", err
	}
	logf("翻译完成：%s", filepath.Base(result))
	return result, nil
}

type translator struct {
	client   openai.Client
	cfg      Config
	logf     Logf
	progress Progress

	// 译文缓存按原文内容寻址：失败重试、试译转全文都直接复用已译部分
	cachePath string
	cache     map[string]string
	mu        sync.Mutex
}

func newTranslator(cfg Config, cachePath string, logf Logf, progress Progress) (*translator, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if cfg.Proxy != "" {
		proxy, err := url.Parse(cfg.Proxy)
		if err != nil {
			return nil, fmt.Errorf("代理地址无效：%w", err)
		}
		transport.Proxy = http.ProxyURL(proxy)
	}
	opts := []option.RequestOption{
		option.WithAPIKey(cfg.APIKey),
		option.WithHTTPClient(&http.Client{Transport: transport}),
		option.WithMaxRetries(0),
		option.WithRequestTimeout(300 * time.Second),
	}
	if cfg.APIBase != "" {
		opts = append(opts, option.WithBaseURL(cfg.APIBase))
	}

	cache := map[string]string{}
	data, err := os.ReadFile(cachePath)
	if err == nil {
		if err := json.Unmarshal(data, &cache); err != nil {
			return nil, fmt.Errorf("译文缓存损坏：%w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}

	return &translator{
		client:    openai.NewClient(opts...),
		cfg:       cfg,
		logf:      logf,
		progress:  progress,
		cachePath: cachePath,
		cache:     cache,
	}, nil
}

func (t *translator) request(ctx context.Context, text, extraSystem string) (string, error) {
	system := t.cfg.PromptSystem
	if extraSystem != "" {
		system += "\n\n" + extraSystem
	}
	user := strings.ReplaceAll(t.cfg.PromptUser, "{language}", t.cfg.Language)
	user = strings.ReplaceAll(user, "{text}", text)
	params := openai.ChatCompletionNewParams{
		Model:    t.cfg.Model,
		Messages: []openai.ChatCompletionMessageParamUnion{openai.SystemMessage(system), openai.UserMessage(user)},
	}

	lastErr := ""
	for attempt := range 6 {
		if attempt > 0 {
			wait := min(60, 5<<(attempt-1))
			t.logf("请求失败（%s），%ds 后重试", lastErr, wait)
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(time.Duration(wait) * time.Second):
			}
		}

		resp, err := t.client.Chat.Completions.New(ctx, params)
		if err != nil {
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			// SDK 没有按类型区分的异常：拿不到 *openai.Error 是连接错误，可重试；否则只重试 429 和 5xx
			var apiErr *openai.Error
			if errors.As(err, &apiErr) && apiErr.StatusCode != http.StatusTooManyRequests && apiErr.StatusCode < 500 {
				return "", err
			}
			lastErr = err.Error()
			continue
		}
		if len(resp.Choices) == 0 {
			lastErr = "空回复"
			continue
		}
		choice := resp.Choices[0]
		content := strings.TrimSpace(choice.Message.Content)
		if content == "" {
			lastErr = "空回复"
			continue
		}
		if choice.FinishReason == "length" {
			t.logf("警告：输出被截断（finish_reason=length），可调小分块或换长输出模型")
		}
		return content, nil
	}
	return "", fmt.Errorf("翻译失败：%s", lastErr)
}

func cacheKey(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// store 调用方持有 t.mu
func (t *translator) store(text, result string) error {
	t.cache[cacheKey(text)] = result
	data, err := json.Marshal(t.cache)
	if err != nil {
		return err
	}
	tmp := strings.TrimSuffix(t.cachePath, filepath.Ext(t.cachePath)) + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, t.cachePath)
}

func (t *translator) translateAll(ctx context.Context, texts []string, extraSystem string) ([]string, error) {
	results := make([]string, len(texts))
	var pending []int
	for i, text := range texts {
		if result, ok := t.cache[cacheKey(text)]; ok {
			results[i] = result
		} else {
			pending = append(pending, i)
		}
	}
	done := len(texts) - len(pending)
	t.logf("共 %d 块，缓存命中 %d 块，待翻译 %d 块", len(texts), done, len(pending))
	t.progress(done, len(texts))

	// 任一块失败就取消 ctx：排队中的块不再发请求，进行中的请求立即中断
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(max(1, t.cfg.Workers))
	for _, i := range pending {
		g.Go(func() error {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			result, err := t.request(ctx, texts[i], extraSystem)
			if err != nil {
				return err
			}
			t.mu.Lock()
			defer t.mu.Unlock()
			results[i] = result
			if err := t.store(texts[i], result); err != nil {
				return err
			}
			done++
			t.logf("第 %d 块完成（%d → %d 字）", i+1, utf8.RuneCountInString(texts[i]), utf8.RuneCountInString(result))
			t.progress(done, len(texts))
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	return results, nil
}
