package app

import (
	"database/sql"
	"encoding/json"
	"time"

	_ "modernc.org/sqlite"
)

const schema = `
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,            -- queued / running / succeeded / failed / canceled
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    upload_name TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL,      -- {"mode", "format", "bilingual", "test_blocks"}
    stage TEXT NOT NULL DEFAULT '',  -- download / translate
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
`

func openDB(path string) (*sql.DB, error) {
	// worker 和请求处理同时读写：WAL 避免读被写阻塞；事务一开始就拿写锁，避免读锁升级时撞上 SQLITE_BUSY
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(30000)&_pragma=journal_mode(WAL)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// now 与旧数据格式一致：UTC，精确到秒，带 +00:00
func now() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05+00:00")
}

type TaskOptions struct {
	Mode       string `json:"mode"`   // download / translate
	Format     string `json:"format"` // txt / epub
	Bilingual  bool   `json:"bilingual"`
	TestBlocks int    `json:"test_blocks"`
}

type Task struct {
	ID              int64       `json:"id"`
	Status          string      `json:"status"`
	Title           string      `json:"title"`
	URL             string      `json:"url"`
	UploadName      string      `json:"upload_name"`
	Options         TaskOptions `json:"options"`
	Stage           string      `json:"stage"`
	ProgressCurrent int         `json:"progress_current"`
	ProgressTotal   int         `json:"progress_total"`
	Error           string      `json:"error"`
	CreatedAt       string      `json:"created_at"`
	StartedAt       *string     `json:"started_at"`
	FinishedAt      *string     `json:"finished_at"`
}

func taskOptionsJSON(o TaskOptions) string {
	data, _ := json.Marshal(o)
	return string(data)
}

const taskColumns = "id, status, title, url, upload_name, options_json, stage, progress_current, progress_total, error, created_at, started_at, finished_at"

type scanner interface {
	Scan(dest ...any) error
}

func scanTask(row scanner) (Task, error) {
	var t Task
	var options string
	err := row.Scan(&t.ID, &t.Status, &t.Title, &t.URL, &t.UploadName, &options, &t.Stage,
		&t.ProgressCurrent, &t.ProgressTotal, &t.Error, &t.CreatedAt, &t.StartedAt, &t.FinishedAt)
	if err != nil {
		return t, err
	}
	return t, json.Unmarshal([]byte(options), &t.Options)
}

type querier interface {
	QueryRow(query string, args ...any) *sql.Row
}

// getTask 查不到时返回 sql.ErrNoRows
func getTask(q querier, id int64) (Task, error) {
	return scanTask(q.QueryRow("SELECT "+taskColumns+" FROM tasks WHERE id = ?", id))
}

func taskStatus(db *sql.DB, id int64) (string, error) {
	var status string
	err := db.QueryRow("SELECT status FROM tasks WHERE id = ?", id).Scan(&status)
	if err == sql.ErrNoRows {
		return "canceled", nil // 已被删除按取消处理
	}
	return status, err
}

type execer interface {
	Exec(query string, args ...any) (sql.Result, error)
}

func addLog(e execer, taskID int64, message string) error {
	_, err := e.Exec("INSERT INTO task_logs (task_id, message, created_at) VALUES (?, ?, ?)", taskID, message, now())
	return err
}
