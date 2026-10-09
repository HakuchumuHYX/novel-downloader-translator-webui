package app

import (
	"database/sql"
	"fmt"
	"slices"
	"strconv"
	"strings"
)

const defaultPromptSystem = "你是一位专业中文小说译者。保持叙事语气、人物称呼和段落结构；" +
	"逐行对应输出，保留空行与以 ● 开头的章节标记行；" +
	"只输出译文，不添加解释、警告或省略标记。"

const defaultPromptUser = "目标语言：{language}\n请逐行对应翻译并保持段落结构：\n\n{text}"

var settingKeys = []string{
	"api_base", "api_key", "model", "language", "prompt_system", "prompt_user",
	"parallel_workers", "proxy", "cookie_syosetu", "cookie_novel18", "cookie_kakuyomu",
}

var settingDefaults = map[string]string{
	"language":         "简体中文",
	"prompt_system":    defaultPromptSystem,
	"prompt_user":      defaultPromptUser,
	"parallel_workers": "5",
}

// 页面上只显示掩码，提交空值表示不修改
var secretKeys = []string{"api_key", "cookie_syosetu", "cookie_novel18", "cookie_kakuyomu"}

func loadSettings(db *sql.DB) (map[string]string, error) {
	values := map[string]string{}
	for _, key := range settingKeys {
		values[key] = settingDefaults[key]
	}
	rows, err := db.Query("SELECT key, value FROM settings")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			return nil, err
		}
		values[key] = value
	}
	return values, rows.Err()
}

func maskSecret(value string) string {
	runes := []rune(value)
	switch {
	case len(runes) == 0:
		return ""
	case len(runes) > 12:
		return fmt.Sprintf("已设置（%s…%s）", string(runes[:4]), string(runes[len(runes)-4:]))
	default:
		return "已设置"
	}
}

// saveSettings 返回的 *httpError 是表单校验失败
func saveSettings(db *sql.DB, form func(string) string) error {
	updates := map[string]string{}
	for _, key := range settingKeys {
		value := strings.TrimSpace(strings.ReplaceAll(form(key), "\r\n", "\n"))
		if slices.Contains(secretKeys, key) {
			if form("clear_"+key) != "" {
				value = ""
			} else if value == "" {
				continue
			}
		}
		updates[key] = value
	}

	if !strings.Contains(updates["prompt_user"], "{text}") {
		return badRequest("用户提示词必须包含 {text}")
	}
	if n, err := strconv.Atoi(updates["parallel_workers"]); err != nil || n < 1 || n > 32 {
		return badRequest("并发数须为 1–32 的整数")
	}

	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for key, value := range updates {
		_, err := tx.Exec("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}
