// Package downloader 抓 syosetu / novel18 / kakuyomu，合并成一个 txt（可转 epub）。
package downloader

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/net/html"
)

var sites = map[string]string{
	"ncode.syosetu.com":   "syosetu",
	"novel18.syosetu.com": "novel18",
	"kakuyomu.jp":         "kakuyomu",
}

const userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

func DetectSite(rawURL string) (string, error) {
	host := ""
	if u, err := url.Parse(rawURL); err == nil {
		host = strings.ToLower(u.Hostname())
	}
	site, ok := sites[host]
	if !ok {
		if host == "" {
			host = rawURL
		}
		return "", fmt.Errorf("不支持的站点：%s", host)
	}
	return site, nil
}

var (
	// Python 版的 \s 是 Unicode 空白，标题里的全角空格也要折叠，文件名才和旧版一致
	unsafeChars = regexp.MustCompile(`[\\/:*?"<>|]+`)
	spaces      = regexp.MustCompile(`[\s\v\x{85}\p{Z}]+`)
)

func SanitizeFilename(name string) string {
	text := unsafeChars.ReplaceAllString(name, "_")
	text = strings.TrimSpace(spaces.ReplaceAllString(text, " "))
	// 文件名上限 255 字节，日文一字 3 字节；按字节截断并给「_译.epub」之类的后缀留余量
	for len(text) > 200 {
		_, size := utf8.DecodeLastRuneInString(text)
		text = text[:len(text)-size]
	}
	if text == "" {
		return "book"
	}
	return text
}

func cookieHeader(raw string, novel18 bool) string {
	var pairs []string
	for _, pair := range strings.Split(raw, ";") {
		key, value, ok := strings.Cut(strings.TrimSpace(pair), "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" || (novel18 && key == "over18") {
			continue
		}
		pairs = append(pairs, key+"="+strings.TrimSpace(value))
	}
	if novel18 {
		pairs = append(pairs, "over18=yes")
	}
	return strings.Join(pairs, "; ")
}

type fetcher struct {
	client *http.Client
	cookie string
}

func newFetcher(proxy, cookie string) (*fetcher, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if proxy != "" {
		u, err := url.Parse(proxy)
		if err != nil {
			return nil, fmt.Errorf("代理地址无效：%w", err)
		}
		transport.Proxy = http.ProxyURL(u)
	}
	return &fetcher{client: &http.Client{Transport: transport, Timeout: 120 * time.Second}, cookie: cookie}, nil
}

// get 几百章并发抓取时偶发断连/5xx 很常见，单请求重试比整本重跑便宜
func (f *fetcher) get(ctx context.Context, rawURL string) (string, error) {
	lastErr := ""
	for attempt := range 4 {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(time.Duration(2<<attempt) * time.Second):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if err != nil {
			return "", err
		}
		req.Header.Set("User-Agent", userAgent)
		if f.cookie != "" {
			req.Header.Set("Cookie", f.cookie)
		}
		resp, err := f.client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			lastErr = err.Error()
			continue
		}
		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		switch {
		case err != nil:
			lastErr = err.Error()
		case resp.StatusCode < 400:
			return string(body), nil
		case resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests:
			return "", fmt.Errorf("HTTP %d：%s", resp.StatusCode, rawURL)
		default:
			lastErr = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
	}
	return "", fmt.Errorf("请求失败（%s）：%s", lastErr, rawURL)
}

// skipText 里的文本 BeautifulSoup 的 get_text 默认不收：rt/rp 是振假名注音和括号，不跳过会变成「魔王（俺）」拆成多行
var skipText = map[string]bool{"script": true, "style": true, "template": true, "rt": true, "rp": true}

// nodeText 对应 BeautifulSoup 的 get_text(sep, strip)
func nodeText(n *html.Node, sep string, strip bool) string {
	var parts []string
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			switch c.Type {
			case html.TextNode:
				text := c.Data
				if strip {
					text = strings.TrimSpace(text)
				}
				if text != "" {
					parts = append(parts, text)
				}
			case html.ElementNode:
				if !skipText[c.Data] {
					walk(c)
				}
			}
		}
	}
	walk(n)
	return strings.Join(parts, sep)
}
