package translator

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

const (
	maxLines = 80
	maxChars = 12000
)

// splitLines 与 Python str.splitlines() 一致，切块结果要和旧版逐字节相同，否则译文缓存全部失效
func splitLines(s string) []string {
	var lines []string
	start := 0
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		switch r {
		case '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', ' ', ' ':
			lines = append(lines, s[start:i])
			i += size
			start = i
		case '\r':
			lines = append(lines, s[start:i])
			i += size
			if i < len(s) && s[i] == '\n' {
				i++
			}
			start = i
		default:
			i += size
		}
	}
	if start < len(s) {
		lines = append(lines, s[start:])
	}
	return lines
}

// splitChunks 按「● 章节标记」和行数/字数上限切块；块内保留原有换行交给模型逐行对应。
func splitChunks(text string) []string {
	var chunks, current []string
	size := 0
	for _, line := range splitLines(text) {
		length := utf8.RuneCountInString(line)
		if len(current) > 0 && (strings.HasPrefix(line, "● ") || len(current) >= maxLines || size+length > maxChars) {
			chunks = append(chunks, strings.Trim(strings.Join(current, "\n"), "\n"))
			current, size = nil, 0
		}
		current = append(current, line)
		size += length + 1
	}
	chunks = append(chunks, strings.Trim(strings.Join(current, "\n"), "\n"))

	var result []string
	for _, chunk := range chunks {
		if strings.TrimSpace(chunk) != "" {
			result = append(result, chunk)
		}
	}
	return result
}

func merge(original, translated string, bilingual bool) string {
	// 模型偶尔丢掉章节标记，补回来才能正确分章
	if strings.HasPrefix(original, "● ") && !strings.HasPrefix(translated, "● ") {
		translated = "● " + translated
	}
	if !bilingual {
		return translated
	}
	if strings.HasPrefix(translated, "● ") {
		// 双语时只保留译文标题作为分章标记，原文标题降为普通行
		titleLine, body, _ := strings.Cut(translated, "\n")
		return titleLine + "\n" + strings.TrimPrefix(original, "● ") + "\n\n" + body
	}
	return original + "\n\n" + translated
}

func translateTxt(ctx context.Context, src, outDir string, t *translator, opts Options) (string, error) {
	data, err := os.ReadFile(src)
	if err != nil {
		return "", err
	}
	chunks := splitChunks(string(data))
	if opts.TestBlocks > 0 && opts.TestBlocks < len(chunks) {
		chunks = chunks[:opts.TestBlocks]
	}
	translations, err := t.translateAll(ctx, chunks, "")
	if err != nil {
		return "", err
	}
	merged := make([]string, len(chunks))
	for i := range chunks {
		merged[i] = merge(chunks[i], translations[i], opts.Bilingual)
	}
	text := strings.Join(merged, "\n\n") + "\n"

	stem := strings.TrimSuffix(filepath.Base(src), filepath.Ext(src))
	if opts.Epub {
		result := filepath.Join(outDir, stem+"_译.epub")
		return result, WriteEpub(text, result, stem, LanguageCode(t.cfg.Language))
	}
	result := filepath.Join(outDir, stem+"_译.txt")
	return result, os.WriteFile(result, []byte(text), 0o644)
}
