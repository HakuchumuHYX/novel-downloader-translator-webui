package translator

import (
	"archive/zip"
	"cmp"
	"fmt"
	"html"
	"os"
	"regexp"
	"strings"
	"time"
)

// LanguageCode 把设置里的目标语言转成 dc:language。
// 阅读器按 dc:language 选字体，中文书标成 ja 会用日文字形显示汉字。
func LanguageCode(language string) string {
	if strings.Contains(language, "繁") {
		return "zh-TW"
	}
	if strings.Contains(language, "中") || strings.HasPrefix(strings.ToLower(language), "zh") {
		return "zh-CN"
	}
	return language
}

type chapter struct {
	title string
	body  string
}

func splitChapters(text, defaultTitle string) []chapter {
	var titles []string
	var bodies [][]string
	for _, line := range splitLines(text) {
		if title, ok := strings.CutPrefix(line, "● "); ok {
			titles = append(titles, cmp.Or(strings.TrimSpace(title), defaultTitle))
			bodies = append(bodies, nil)
			continue
		}
		if len(titles) == 0 {
			titles = append(titles, defaultTitle)
			bodies = append(bodies, nil)
		}
		bodies[len(bodies)-1] = append(bodies[len(bodies)-1], line)
	}
	chapters := make([]chapter, len(titles))
	for i := range titles {
		chapters[i] = chapter{titles[i], strings.TrimSpace(strings.Join(bodies[i], "\n"))}
	}
	return chapters
}

// Python 版用的是 \n\s*\n，Python 的 \s 含全角空格等 Unicode 空白
var blankLine = regexp.MustCompile(`\n[\s\v\p{Z}]*\n`)

func bodyHTML(body string) string {
	var b strings.Builder
	for _, part := range blankLine.Split(body, -1) {
		if strings.TrimSpace(part) == "" {
			continue
		}
		lines := splitLines(part)
		for i, line := range lines {
			lines[i] = html.EscapeString(line)
		}
		b.WriteString("<p>" + strings.Join(lines, "<br/>") + "</p>")
	}
	return b.String()
}

const containerXML = `<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
`

const chapterXHTML = `<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="%[1]s" xml:lang="%[1]s">
<head><title>%[2]s</title></head>
<body><h1>%[2]s</h1>%[3]s</body>
</html>
`

// WriteEpub 把以「● 标题」分章的 txt 写成 epub3（带 ncx 兼容老阅读器）。
func WriteEpub(text, path, title, language string) error {
	chapters := splitChapters(text, title)
	lang := html.EscapeString(language)
	bookTitle := html.EscapeString(title)

	var manifest, spine, navItems, navPoints strings.Builder
	var chapterFiles [][2]string
	for i, ch := range chapters {
		id := fmt.Sprintf("chap_%04d", i+1)
		name := id + ".xhtml"
		chTitle := html.EscapeString(ch.title)
		chapterFiles = append(chapterFiles, [2]string{"EPUB/" + name, fmt.Sprintf(chapterXHTML, lang, chTitle, bodyHTML(ch.body))})
		fmt.Fprintf(&manifest, "    <item id=\"%s\" href=\"%s\" media-type=\"application/xhtml+xml\"/>\n", id, name)
		fmt.Fprintf(&spine, "    <itemref idref=\"%s\"/>\n", id)
		fmt.Fprintf(&navItems, "      <li><a href=\"%s\">%s</a></li>\n", name, chTitle)
		fmt.Fprintf(&navPoints, "    <navPoint id=\"%s\" playOrder=\"%d\"><navLabel><text>%s</text></navLabel><content src=\"%s\"/></navPoint>\n", id, i+1, chTitle, name)
	}
	identifier := html.EscapeString("translator-webui-" + title)

	opf := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id" xml:lang="%[1]s">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">%[2]s</dc:identifier>
    <dc:title>%[3]s</dc:title>
    <dc:language>%[1]s</dc:language>
    <meta property="dcterms:modified">%[4]s</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
%[5]s  </manifest>
  <spine toc="ncx">
    <itemref idref="nav"/>
%[6]s  </spine>
</package>
`, lang, identifier, bookTitle, time.Now().UTC().Format("2006-01-02T15:04:05Z"), manifest.String(), spine.String())

	nav := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="%[1]s" xml:lang="%[1]s">
<head><title>%[2]s</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h2>%[2]s</h2>
    <ol>
%[3]s    </ol>
  </nav>
</body>
</html>
`, lang, bookTitle, navItems.String())

	ncx := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="%s"/></head>
  <docTitle><text>%s</text></docTitle>
  <navMap>
%s  </navMap>
</ncx>
`, identifier, bookTitle, navPoints.String())

	out, err := os.Create(path)
	if err != nil {
		return err
	}
	defer out.Close()
	zw := zip.NewWriter(out)
	// mimetype 必须是第一项且不压缩，否则阅读器认不出是 epub
	w, err := zw.CreateHeader(&zip.FileHeader{Name: "mimetype", Method: zip.Store})
	if err != nil {
		return err
	}
	if _, err := w.Write([]byte("application/epub+zip")); err != nil {
		return err
	}
	files := append([][2]string{
		{"META-INF/container.xml", containerXML},
		{"EPUB/content.opf", opf},
		{"EPUB/nav.xhtml", nav},
		{"EPUB/toc.ncx", ncx},
	}, chapterFiles...)
	for _, file := range files {
		w, err := zw.Create(file[0])
		if err != nil {
			return err
		}
		if _, err := w.Write([]byte(file[1])); err != nil {
			return err
		}
	}
	if err := zw.Close(); err != nil {
		return err
	}
	return out.Close()
}
