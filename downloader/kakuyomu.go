package downloader

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

// カクヨム。作品页 https://kakuyomu.jp/works/<id>。

var (
	kakuyomuWork     = regexp.MustCompile(`/works/([^/?#]+)`)
	kakuyomuNextData = regexp.MustCompile(`(?s)<script id="__NEXT_DATA__" type="application/json">(.*?)</script>`)

	kakuyomuTitleSelectors = []string{"h1#contentMain-header", "p.widget-episodeTitle", "h1"}
	kakuyomuBodySelectors  = []string{
		"div.widget-episodeBody",
		"div#contentMain-inner",
		"div.js-episode-body",
		"div[itemprop='articleBody']",
	}
)

type apolloRef struct {
	Ref string `json:"__ref"`
}

func kakuyomuIndex(ctx context.Context, f *fetcher, rawURL string) (string, []chapterRef, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return "", nil, err
	}
	m := kakuyomuWork.FindStringSubmatch(parsed.Path)
	if m == nil {
		return "", nil, fmt.Errorf("不是カクヨム作品链接：%s", rawURL)
	}
	workID := m[1]
	workURL := "https://kakuyomu.jp/works/" + workID

	// 作品页是 Next.js 渲染的，HTML 里的目录会折叠（180 话只露出 7 个链接），
	// 完整目录在 __NEXT_DATA__ 的 Apollo state 里：Work → tableOfContentsV2 → episodeUnions
	page, err := f.get(ctx, workURL)
	if err != nil {
		return "", nil, err
	}
	data := kakuyomuNextData.FindStringSubmatch(page)
	if data == nil {
		return "", nil, fmt.Errorf("作品页里找不到 __NEXT_DATA__，カクヨム页面结构可能变了")
	}
	var next struct {
		Props struct {
			PageProps struct {
				State map[string]json.RawMessage `json:"__APOLLO_STATE__"`
			} `json:"pageProps"`
		} `json:"props"`
	}
	if err := json.Unmarshal([]byte(data[1]), &next); err != nil {
		return "", nil, fmt.Errorf("解析 __NEXT_DATA__ 失败：%w", err)
	}
	state := next.Props.PageProps.State
	lookup := func(key string, v any) error {
		raw, ok := state[key]
		if !ok {
			return fmt.Errorf("Apollo state 里没有 %s，カクヨム页面结构可能变了", key)
		}
		return json.Unmarshal(raw, v)
	}

	var work struct {
		Title             string      `json:"title"`
		TableOfContentsV2 []apolloRef `json:"tableOfContentsV2"`
	}
	if err := lookup("Work:"+workID, &work); err != nil {
		return "", nil, err
	}
	var chapters []chapterRef
	for _, tocRef := range work.TableOfContentsV2 {
		var toc struct {
			EpisodeUnions []apolloRef `json:"episodeUnions"`
		}
		if err := lookup(tocRef.Ref, &toc); err != nil {
			return "", nil, err
		}
		for _, episodeRef := range toc.EpisodeUnions {
			var episode struct {
				ID string `json:"id"`
			}
			if err := lookup(episodeRef.Ref, &episode); err != nil {
				return "", nil, err
			}
			chapters = append(chapters, chapterRef{episode.ID, workURL + "/episodes/" + episode.ID})
		}
	}
	return work.Title, chapters, nil
}

func kakuyomuChapter(ctx context.Context, f *fetcher, chapterURL string) (string, string, error) {
	page, err := f.get(ctx, chapterURL)
	if err != nil {
		return "", "", err
	}
	doc, err := parseHTML(page)
	if err != nil {
		return "", "", err
	}
	title := chapterURL[strings.LastIndex(chapterURL, "/")+1:]
	for _, sel := range kakuyomuTitleSelectors {
		if node := doc.Find(sel).First(); node.Length() > 0 {
			if text := nodeText(node.Get(0), "", true); text != "" {
				title = text
				break
			}
		}
	}
	content := ""
	for _, sel := range kakuyomuBodySelectors {
		if node := doc.Find(sel).First(); node.Length() > 0 {
			if text := nodeText(node.Get(0), "\n", true); text != "" {
				content = text
				break
			}
		}
	}
	if content == "" {
		return "", "", fmt.Errorf("解析章节失败：%s", chapterURL)
	}
	return title, content, nil
}
