package downloader

import (
	"context"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/PuerkitoBio/goquery"
	"golang.org/x/sync/errgroup"
)

// 小説家になろう / ノクターン（novel18）。novel18 需要登录 Cookie + over18=yes。

func parseHTML(page string) (*goquery.Document, error) {
	return goquery.NewDocumentFromReader(strings.NewReader(page))
}

func syosetuIndex(ctx context.Context, f *fetcher, rawURL string) (string, []chapterRef, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return "", nil, err
	}
	base := "https://" + parsed.Hostname()
	novelID := ""
	for _, seg := range strings.Split(parsed.Path, "/") {
		if seg != "" {
			novelID = strings.ToLower(seg)
			break
		}
	}
	if novelID == "" {
		return "", nil, fmt.Errorf("不是作品链接：%s", rawURL)
	}
	indexURL := base + "/" + novelID + "/"

	page, err := f.get(ctx, indexURL)
	if err != nil {
		return "", nil, err
	}
	first, err := parseHTML(page)
	if err != nil {
		return "", nil, err
	}
	titleNode := first.Find("h1.p-novel__title").First()
	if titleNode.Length() == 0 {
		return "", nil, fmt.Errorf("找不到作品标题，novel18 请检查 Cookie 是否有效")
	}
	title := nodeText(titleNode.Get(0), "", true)

	// 目录分页：?p=2, ?p=3 ...
	maxPage := 1
	first.Find("a[href]").Each(func(_ int, a *goquery.Selection) {
		href, _ := url.Parse(a.AttrOr("href", ""))
		if href == nil {
			return
		}
		for _, p := range href.Query()["p"] {
			if n, err := strconv.Atoi(p); err == nil && n > maxPage {
				maxPage = n
			}
		}
	})
	docs := make([]*goquery.Document, maxPage)
	docs[0] = first
	g, gctx := errgroup.WithContext(ctx)
	for p := 2; p <= maxPage; p++ {
		g.Go(func() error {
			page, err := f.get(gctx, fmt.Sprintf("%s?p=%d", indexURL, p))
			if err != nil {
				return err
			}
			docs[p-1], err = parseHTML(page)
			return err
		})
	}
	if err := g.Wait(); err != nil {
		return "", nil, err
	}

	pattern := regexp.MustCompile(`/` + regexp.QuoteMeta(novelID) + `/(\d+)/?$`)
	seen := map[int]bool{}
	for _, doc := range docs {
		doc.Find("a[href]").Each(func(_ int, a *goquery.Selection) {
			if m := pattern.FindStringSubmatch(a.AttrOr("href", "")); m != nil {
				n, _ := strconv.Atoi(m[1])
				seen[n] = true
			}
		})
	}
	if len(seen) == 0 {
		// 短篇没有目录，正文直接在作品首页
		return title, []chapterRef{{"1", indexURL}}, nil
	}
	numbers := make([]int, 0, len(seen))
	for n := range seen {
		numbers = append(numbers, n)
	}
	slices.Sort(numbers)
	chapters := make([]chapterRef, len(numbers))
	for i, n := range numbers {
		chapters[i] = chapterRef{strconv.Itoa(n), fmt.Sprintf("%s/%s/%d/", base, novelID, n)}
	}
	return title, chapters, nil
}

func syosetuChapter(ctx context.Context, f *fetcher, chapterURL string) (string, string, error) {
	page, err := f.get(ctx, chapterURL)
	if err != nil {
		return "", "", err
	}
	doc, err := parseHTML(page)
	if err != nil {
		return "", "", err
	}
	titleNode := doc.Find("h1.p-novel__title").First()
	bodyNode := doc.Find("div.p-novel__body").First()
	if titleNode.Length() == 0 || bodyNode.Length() == 0 {
		return "", "", fmt.Errorf("解析章节失败：%s", chapterURL)
	}
	title := strings.TrimSpace(strings.ReplaceAll(nodeText(titleNode.Get(0), "", false), "　", " "))
	// 全角空格是段首缩进，去掉后交给译文自己排版
	content := strings.TrimSpace(strings.ReplaceAll(nodeText(bodyNode.Get(0), "", false), "　", ""))
	return title, content, nil
}
