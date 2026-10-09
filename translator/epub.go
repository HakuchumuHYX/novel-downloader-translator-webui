package translator

import (
	"archive/zip"
	"context"
	"encoding/xml"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/beevik/etree"
)

// 翻译上传的 epub：直接按 zip 处理，只改正文 xhtml 里的段落/标题文本，图片、目录、元数据原样拷贝。
// XHTML 必须用 XML 解析器往返；HTML5 解析器写回会丢 XML 声明、命名空间并把 <br/> 改成 <br>。

const (
	batchChars   = 4000
	batchItems   = 40
	numberedRule = "输入的每一行以 [编号] 开头。逐行翻译，输出行数与输入相同，每行保留原来的 [编号] 前缀。"
)

var (
	textTags = map[string]bool{"p": true, "h1": true, "h2": true, "h3": true, "h4": true, "h5": true, "h6": true}
	// Python 版的 \s 是 Unicode 空白，提取规则不一致缓存就对不上
	innerNewline = regexp.MustCompile(`[\s\v\x{85}\p{Z}]*\n[\s\v\x{85}\p{Z}]*`)
	numberedLine = regexp.MustCompile(`^[\s\v\x{85}\p{Z}]*\[(\d+)\][\s\v\x{85}\p{Z}]*(.*)`)
	opfLanguage  = regexp.MustCompile(`(<dc:language[^>]*>)[^<]*(</dc:language>)`)
)

type paragraph struct {
	node *etree.Element
	text string
}

func hasTextTagDescendant(node *etree.Element) bool {
	for _, child := range node.ChildElements() {
		if textTags[child.Tag] || hasTextTagDescendant(child) {
			return true
		}
	}
	return false
}

// collect 按文档顺序收集段落；嵌套时只翻最内层
func collect(node *etree.Element, out *[]paragraph) {
	if textTags[node.Tag] && !hasTextTagDescendant(node) {
		if text := nodeText(node); text != "" {
			*out = append(*out, paragraph{node, text})
		}
		return
	}
	for _, child := range node.ChildElements() {
		collect(child, out)
	}
}

func nodeText(node *etree.Element) string {
	var b strings.Builder
	var walk func(*etree.Element)
	walk = func(e *etree.Element) {
		for _, token := range e.Child {
			switch t := token.(type) {
			case *etree.CharData:
				b.WriteString(t.Data)
			case *etree.Element:
				if t.Tag != "rt" && t.Tag != "rp" { // 去掉振假名，否则会变成「漢字かんじ」
					walk(t)
				}
			}
		}
	}
	walk(node)
	return strings.TrimSpace(innerNewline.ReplaceAllString(b.String(), " "))
}

func makeBatches(paragraphs []paragraph) [][]paragraph {
	var batches [][]paragraph
	var current []paragraph
	size := 0
	for _, p := range paragraphs {
		length := utf8.RuneCountInString(p.text)
		if len(current) > 0 && (len(current) >= batchItems || size+length > batchChars) {
			batches = append(batches, current)
			current, size = nil, 0
		}
		current = append(current, p)
		size += length
	}
	if len(current) > 0 {
		batches = append(batches, current)
	}
	return batches
}

func numbered(batch []paragraph) string {
	lines := make([]string, len(batch))
	for i, p := range batch {
		lines[i] = fmt.Sprintf("[%d] %s", i+1, p.text)
	}
	return strings.Join(lines, "\n")
}

// parseNumbered 编号必须恰好是 1..count，否则返回 nil
func parseNumbered(result string, count int) []string {
	lines := map[int]string{}
	for _, line := range splitLines(result) {
		if m := numberedLine.FindStringSubmatch(line); m != nil {
			n, _ := strconv.Atoi(m[1])
			lines[n] = strings.TrimSpace(m[2])
		}
	}
	if len(lines) != count {
		return nil
	}
	parsed := make([]string, count)
	for i := range count {
		line, ok := lines[i+1]
		if !ok {
			return nil
		}
		parsed[i] = line
	}
	return parsed
}

func apply(node *etree.Element, translated string, bilingual bool) {
	if bilingual {
		added := etree.NewElement(node.Tag)
		added.Space = node.Space
		for _, attr := range node.Attr {
			if attr.Space == "" && attr.Key == "id" {
				continue // 重复 id 会破坏目录跳转
			}
			added.CreateAttr(attr.FullKey(), attr.Value)
		}
		added.SetText(translated)
		node.Parent().InsertChildAt(node.Index()+1, added)
		return
	}
	for len(node.Child) > 0 {
		node.RemoveChildAt(0)
	}
	node.SetText(translated)
}

func readZipFile(f *zip.File) ([]byte, error) {
	r, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return io.ReadAll(r)
}

func translateEpub(ctx context.Context, src, outDir string, t *translator, bilingual bool, testBlocks int) (string, error) {
	zin, err := zip.OpenReader(src)
	if err != nil {
		return "", err
	}
	defer zin.Close()

	docs := map[string]*etree.Document{}
	var paragraphs []paragraph
	for _, f := range zin.File {
		ext := strings.ToLower(filepath.Ext(f.Name))
		if ext != ".xhtml" && ext != ".html" && ext != ".htm" {
			continue
		}
		data, err := readZipFile(f)
		if err != nil {
			return "", err
		}
		doc := etree.NewDocument()
		doc.ReadSettings.Entity = xml.HTMLEntity // XHTML 里常见 &nbsp; 之类的实体
		doc.ReadSettings.PreserveCData = true
		if err := doc.ReadFromBytes(data); err != nil {
			return "", fmt.Errorf("解析 %s 失败：%w", f.Name, err)
		}
		docs[f.Name] = doc
		if root := doc.Root(); root != nil {
			collect(root, &paragraphs)
		}
	}

	batches := makeBatches(paragraphs)
	if testBlocks > 0 && testBlocks < len(batches) {
		batches = batches[:testBlocks]
	}
	texts := make([]string, len(batches))
	for i, batch := range batches {
		texts[i] = numbered(batch)
	}
	results, err := t.translateAll(ctx, texts, numberedRule)
	if err != nil {
		return "", err
	}

	var fallback []paragraph
	for i, batch := range batches {
		lines := parseNumbered(results[i], len(batch))
		if lines == nil {
			fallback = append(fallback, batch...)
			continue
		}
		for j, p := range batch {
			apply(p.node, lines[j], bilingual)
		}
	}
	if len(fallback) > 0 {
		// 编号对不上的批次退回逐段翻译，慢但不会错位
		t.logf("%d 段编号未对齐，改为逐段翻译", len(fallback))
		texts := make([]string, len(fallback))
		for i, p := range fallback {
			texts[i] = p.text
		}
		lines, err := t.translateAll(ctx, texts, "")
		if err != nil {
			return "", err
		}
		for i, p := range fallback {
			apply(p.node, lines[i], bilingual)
		}
	}

	stem := strings.TrimSuffix(filepath.Base(src), filepath.Ext(src))
	result := filepath.Join(outDir, stem+"_译.epub")
	out, err := os.Create(result)
	if err != nil {
		return "", err
	}
	defer out.Close()
	zw := zip.NewWriter(out)
	lang := "${1}" + LanguageCode(t.cfg.Language) + "${2}"
	// 原顺序写回，保证 mimetype 仍是第一个且不压缩
	for _, f := range zin.File {
		var data []byte
		if doc, ok := docs[f.Name]; ok {
			data, err = doc.WriteToBytes()
		} else {
			data, err = readZipFile(f)
			if err == nil && strings.HasSuffix(strings.ToLower(f.Name), ".opf") {
				data = opfLanguage.ReplaceAll(data, []byte(lang))
			}
		}
		if err != nil {
			return "", err
		}
		method := zip.Deflate
		if f.Name == "mimetype" {
			method = zip.Store
		}
		w, err := zw.CreateHeader(&zip.FileHeader{Name: f.Name, Method: method, Modified: f.Modified})
		if err != nil {
			return "", err
		}
		if _, err := w.Write(data); err != nil {
			return "", err
		}
	}
	if err := zw.Close(); err != nil {
		return "", err
	}
	return result, out.Close()
}
