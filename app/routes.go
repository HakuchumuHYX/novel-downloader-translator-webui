// Package app 是 webui：页面、接口、SQLite 和任务队列。
package app

import (
	"bytes"
	"cmp"
	"context"
	"crypto/subtle"
	"database/sql"
	"embed"
	"encoding/json"
	"errors"
	"html/template"
	"io"
	"io/fs"
	"log"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/HakuchumuHYX/novel-downloader-translator-webui/downloader"
)

//go:embed templates static
var assets embed.FS

var statusLabels = map[string]string{
	"queued":    "排队中",
	"running":   "进行中",
	"succeeded": "已完成",
	"failed":    "失败",
	"canceled":  "已停止",
}

type server struct {
	db           *sql.DB
	taskDir      string
	authUser     string
	authPassword string
	worker       *worker
	pages        map[string]*template.Template
}

// Run 启动任务队列和 HTTP 服务，ctx 取消后优雅退出。
func Run(ctx context.Context, dataDir, authUser, authPassword, addr string) error {
	taskDir := filepath.Join(dataDir, "tasks")
	if err := os.MkdirAll(taskDir, 0o755); err != nil {
		return err
	}
	db, err := openDB(filepath.Join(dataDir, "app.sqlite3"))
	if err != nil {
		return err
	}
	defer db.Close()
	// 上次进程退出时还在跑的任务已经中断；重试会从缓存续跑
	_, err = db.Exec("UPDATE tasks SET status = 'failed', error = '服务重启导致中断，点重试可续跑', finished_at = ? "+
		"WHERE status = 'running'", now())
	if err != nil {
		return err
	}
	pages, err := parsePages()
	if err != nil {
		return err
	}

	s := &server{
		db:           db,
		taskDir:      taskDir,
		authUser:     authUser,
		authPassword: authPassword,
		worker:       newWorker(db, taskDir),
		pages:        pages,
	}
	go s.worker.run(ctx)

	srv := &http.Server{Addr: addr, Handler: s.routes()}
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()
	log.Printf("listening on %s", addr)
	if err := srv.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	<-stopped
	return nil
}

func (s *server) routes() http.Handler {
	private := http.NewServeMux()
	private.HandleFunc("GET /{$}", s.handle(s.indexPage))
	private.HandleFunc("GET /tasks/{id}", s.handle(s.taskPage))
	private.HandleFunc("GET /settings", s.handle(s.settingsPage))
	private.HandleFunc("GET /tasks/{id}/files/{name}", s.handle(s.downloadFile))

	private.HandleFunc("GET /api/tasks", s.handle(s.listTasksAPI))
	private.HandleFunc("POST /api/tasks", s.mutating(s.createTask))
	private.HandleFunc("GET /api/tasks/{id}", s.handle(s.taskState))
	private.HandleFunc("POST /api/tasks/{id}/stop", s.mutating(s.stopTask))
	private.HandleFunc("POST /api/tasks/{id}/retry", s.mutating(func(w http.ResponseWriter, r *http.Request) error {
		return s.requeue(w, r, false)
	}))
	private.HandleFunc("POST /api/tasks/{id}/full", s.mutating(func(w http.ResponseWriter, r *http.Request) error {
		return s.requeue(w, r, true)
	}))
	private.HandleFunc("DELETE /api/tasks/{id}", s.mutating(s.deleteTask))
	private.HandleFunc("POST /api/settings", s.mutating(s.saveSettings))

	static, _ := fs.Sub(assets, "static")
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServerFS(static)))
	mux.Handle("/", s.requireAuth(private))
	return mux
}

// ---------- 通用 ----------

// httpError 以 {"detail": ...} 返回给页面，页面 JS 用它弹提示
type httpError struct {
	code   int
	detail string
}

func (e *httpError) Error() string { return e.detail }

func badRequest(detail string) error { return &httpError{http.StatusBadRequest, detail} }

func conflict(detail string) error { return &httpError{http.StatusConflict, detail} }

func notFound(detail string) error { return &httpError{http.StatusNotFound, detail} }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

var ok = map[string]bool{"ok": true}

func (s *server) handle(h func(http.ResponseWriter, *http.Request) error) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		err := h(w, r)
		if err == nil {
			return
		}
		var he *httpError
		if errors.As(err, &he) {
			writeJSON(w, he.code, map[string]string{"detail": he.detail})
			return
		}
		log.Printf("%s %s：%v", r.Method, r.URL.Path, err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"detail": "服务器内部错误"})
	}
}

// mutating 变更类请求只接受页面 JS 发出的请求：跨站表单带不上这个头，算是最简单的 CSRF 防护
func (s *server) mutating(h func(http.ResponseWriter, *http.Request) error) http.HandlerFunc {
	return s.handle(func(w http.ResponseWriter, r *http.Request) error {
		if r.Header.Get("X-Requested-With") != "fetch" {
			return &httpError{http.StatusForbidden, "缺少 X-Requested-With: fetch"}
		}
		return h(w, r)
	})
}

func (s *server) requireAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, password, _ := r.BasicAuth()
		userOK := subtle.ConstantTimeCompare([]byte(user), []byte(s.authUser)) == 1
		passwordOK := subtle.ConstantTimeCompare([]byte(password), []byte(s.authPassword)) == 1
		if !userOK || !passwordOK {
			w.Header().Set("WWW-Authenticate", "Basic")
			writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "认证失败"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

func pathID(r *http.Request) (int64, error) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		return 0, notFound("任务不存在")
	}
	return id, nil
}

func taskOr404(q querier, id int64) (Task, error) {
	task, err := getTask(q, id)
	if err == sql.ErrNoRows {
		return task, notFound("任务不存在")
	}
	return task, err
}

type outFile struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
}

func (s *server) outFiles(id int64) []outFile {
	files := []outFile{}
	entries, _ := os.ReadDir(filepath.Join(s.taskDir, strconv.FormatInt(id, 10), "out")) // 目录还不存在就是没有产物
	for _, entry := range entries {
		info, err := entry.Info()
		if err == nil && info.Mode().IsRegular() {
			files = append(files, outFile{entry.Name(), info.Size()})
		}
	}
	return files
}

func (s *server) listTasks() ([]Task, error) {
	rows, err := s.db.Query("SELECT " + taskColumns + " FROM tasks ORDER BY id DESC LIMIT 200")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	tasks := []Task{}
	for rows.Next() {
		task, err := scanTask(rows)
		if err != nil {
			return nil, err
		}
		tasks = append(tasks, task)
	}
	return tasks, rows.Err()
}

// ---------- 页面 ----------

type secretField struct {
	Key, Label, Hint, Type, Masked string
}

func parsePages() (map[string]*template.Template, error) {
	funcs := template.FuncMap{
		"statusLabels": func() map[string]string { return statusLabels },
		"secret": func(key, label, hint, typ string, secrets map[string]string) secretField {
			return secretField{key, label, hint, typ, secrets[key]}
		},
	}
	pages := map[string]*template.Template{}
	// html/template 没有 extends：每个页面和 base.html 单独组成一个模板集，否则各页的 block 互相覆盖
	for _, name := range []string{"index.html", "task.html", "settings.html"} {
		t, err := template.New(name).Funcs(funcs).ParseFS(assets, "templates/base.html", "templates/"+name)
		if err != nil {
			return nil, err
		}
		pages[name] = t
	}
	return pages, nil
}

func (s *server) render(w http.ResponseWriter, name string, data any) error {
	var buf bytes.Buffer
	if err := s.pages[name].ExecuteTemplate(&buf, "base.html", data); err != nil {
		return err
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, err := buf.WriteTo(w)
	return err
}

func (s *server) indexPage(w http.ResponseWriter, r *http.Request) error {
	cfg, err := loadSettings(s.db)
	if err != nil {
		return err
	}
	tasks, err := s.listTasks()
	if err != nil {
		return err
	}
	return s.render(w, "index.html", map[string]any{
		"Tasks":    tasks,
		"APIReady": cfg["api_key"] != "" && cfg["model"] != "",
	})
}

func (s *server) taskPage(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r)
	if err != nil {
		return err
	}
	task, err := taskOr404(s.db, id)
	if err != nil {
		return err
	}
	return s.render(w, "task.html", map[string]any{"Task": task})
}

func (s *server) settingsPage(w http.ResponseWriter, r *http.Request) error {
	cfg, err := loadSettings(s.db)
	if err != nil {
		return err
	}
	secrets := map[string]string{}
	for _, key := range secretKeys {
		secrets[key] = maskSecret(cfg[key])
		delete(cfg, key)
	}
	return s.render(w, "settings.html", map[string]any{"Cfg": cfg, "Secrets": secrets})
}

func (s *server) downloadFile(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r)
	if err != nil {
		return err
	}
	name := r.PathValue("name")
	found := false
	for _, f := range s.outFiles(id) {
		found = found || f.Name == name
	}
	if !found {
		return notFound("文件不存在")
	}
	f, err := os.Open(filepath.Join(s.taskDir, strconv.FormatInt(id, 10), "out", name))
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
	http.ServeContent(w, r, name, info.ModTime(), f)
	return nil
}

// ---------- 任务接口 ----------

func (s *server) listTasksAPI(w http.ResponseWriter, r *http.Request) error {
	tasks, err := s.listTasks()
	if err != nil {
		return err
	}
	writeJSON(w, http.StatusOK, tasks)
	return nil
}

func (s *server) createTask(w http.ResponseWriter, r *http.Request) error {
	if err := r.ParseMultipartForm(32 << 20); err != nil {
		return badRequest("表单解析失败：" + err.Error())
	}
	rawURL := strings.TrimSpace(r.FormValue("url"))
	opts := TaskOptions{
		Mode:      cmp.Or(r.FormValue("mode"), "translate"),
		Format:    cmp.Or(r.FormValue("format"), "txt"),
		Bilingual: r.FormValue("bilingual") == "true",
	}
	if opts.Mode != "download" && opts.Mode != "translate" {
		return badRequest("mode 只能是 download 或 translate")
	}
	if opts.Format != "txt" && opts.Format != "epub" {
		return badRequest("format 只能是 txt 或 epub")
	}
	if value := r.FormValue("test_blocks"); value != "" {
		n, err := strconv.Atoi(value)
		if err != nil || n < 0 {
			return badRequest("试译块数须为非负整数")
		}
		opts.TestBlocks = n
	}
	file, header, err := r.FormFile("file")
	if err != nil && !errors.Is(err, http.ErrMissingFile) {
		return err
	}
	if file != nil {
		defer file.Close()
	}
	hasFile := file != nil && header.Filename != ""
	if (rawURL != "") == hasFile {
		return badRequest("链接和文件二选一")
	}

	cfg, err := loadSettings(s.db)
	if err != nil {
		return err
	}
	var title, uploadName string
	if rawURL != "" {
		site, err := downloader.DetectSite(rawURL)
		if err != nil {
			return badRequest(err.Error())
		}
		if site == "novel18" && cfg["cookie_novel18"] == "" {
			return badRequest("novel18 没有 Cookie 下载不了，请先在设置页填写")
		}
		title = rawURL
	} else {
		original := filepath.Base(header.Filename)
		ext := filepath.Ext(original)
		suffix := strings.ToLower(ext)
		if suffix != ".txt" && suffix != ".epub" {
			return badRequest("只支持 txt 和 epub")
		}
		title = downloader.SanitizeFilename(strings.TrimSuffix(original, ext))
		uploadName = title + suffix
		opts.Mode = "translate"
		if suffix == ".epub" {
			opts.Format = "epub"
		}
	}
	if opts.Mode == "translate" && (cfg["api_key"] == "" || cfg["model"] == "") {
		return badRequest("请先在设置页填写 API Key 和模型")
	}

	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	res, err := tx.Exec("INSERT INTO tasks (status, title, url, upload_name, options_json, created_at) VALUES ('queued', ?, ?, ?, ?, ?)",
		title, rawURL, uploadName, taskOptionsJSON(opts), now())
	if err != nil {
		return err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return err
	}
	// 在同一事务里落盘上传文件：提交前 worker 看不到这条任务
	if hasFile {
		inputDir := filepath.Join(s.taskDir, strconv.FormatInt(id, 10), "input")
		if err := os.MkdirAll(inputDir, 0o755); err != nil {
			return err
		}
		dst, err := os.Create(filepath.Join(inputDir, uploadName))
		if err != nil {
			return err
		}
		_, err = io.Copy(dst, file)
		if closeErr := dst.Close(); err == nil {
			err = closeErr
		}
		if err != nil {
			return err
		}
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	s.worker.notify()
	writeJSON(w, http.StatusOK, map[string]int64{"id": id})
	return nil
}

type taskLog struct {
	ID        int64  `json:"id"`
	Message   string `json:"message"`
	CreatedAt string `json:"created_at"`
}

func (s *server) taskState(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r)
	if err != nil {
		return err
	}
	after := int64(0)
	if value := r.URL.Query().Get("after"); value != "" {
		if after, err = strconv.ParseInt(value, 10, 64); err != nil {
			return badRequest("after 须为整数")
		}
	}
	task, err := taskOr404(s.db, id)
	if err != nil {
		return err
	}

	var rows *sql.Rows
	if after > 0 {
		rows, err = s.db.Query("SELECT id, message, created_at FROM task_logs WHERE task_id = ? AND id > ? ORDER BY id LIMIT 500", id, after)
	} else {
		rows, err = s.db.Query("SELECT id, message, created_at FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 300", id)
	}
	if err != nil {
		return err
	}
	defer rows.Close()
	logs := []taskLog{}
	for rows.Next() {
		var l taskLog
		if err := rows.Scan(&l.ID, &l.Message, &l.CreatedAt); err != nil {
			return err
		}
		logs = append(logs, l)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if after == 0 {
		for i, j := 0, len(logs)-1; i < j; i, j = i+1, j-1 {
			logs[i], logs[j] = logs[j], logs[i]
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": task, "files": s.outFiles(id), "logs": logs})
	return nil
}

func (s *server) stopTask(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r)
	if err != nil {
		return err
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := taskOr404(tx, id); err != nil {
		return err
	}
	res, err := tx.Exec("UPDATE tasks SET status = 'canceled', finished_at = ? WHERE id = ? AND status IN ('queued', 'running')", now(), id)
	if err != nil {
		return err
	}
	if changed, _ := res.RowsAffected(); changed == 0 {
		return conflict("任务没有在运行")
	}
	if err := addLog(tx, id, "已手动停止"); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	s.worker.stopTask(id)
	writeJSON(w, http.StatusOK, ok)
	return nil
}

func (s *server) requeue(w http.ResponseWriter, r *http.Request, full bool) error {
	id, err := pathID(r)
	if err != nil {
		return err
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	task, err := taskOr404(tx, id)
	if err != nil {
		return err
	}
	if task.Status == "queued" || task.Status == "running" {
		return conflict("任务还没结束")
	}
	message := "重试，重新排队"
	if full {
		if task.Options.Mode != "translate" || task.Options.TestBlocks == 0 {
			return conflict("只有试译任务能转全文")
		}
		task.Options.TestBlocks = 0
		message = "转全文，重新排队"
	}
	_, err = tx.Exec("UPDATE tasks SET status = 'queued', options_json = ?, error = '', finished_at = NULL WHERE id = ?",
		taskOptionsJSON(task.Options), id)
	if err != nil {
		return err
	}
	if err := addLog(tx, id, message); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	s.worker.notify()
	writeJSON(w, http.StatusOK, ok)
	return nil
}

func (s *server) deleteTask(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r)
	if err != nil {
		return err
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	task, err := taskOr404(tx, id)
	if err != nil {
		return err
	}
	if task.Status == "running" {
		return conflict("先停止任务再删除")
	}
	if _, err := tx.Exec("DELETE FROM task_logs WHERE task_id = ?", id); err != nil {
		return err
	}
	if _, err := tx.Exec("DELETE FROM tasks WHERE id = ?", id); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	if err := os.RemoveAll(filepath.Join(s.taskDir, strconv.FormatInt(id, 10))); err != nil {
		log.Printf("删除任务 %d 的目录失败：%v", id, err)
	}
	writeJSON(w, http.StatusOK, ok)
	return nil
}

// ---------- 设置接口 ----------

func (s *server) saveSettings(w http.ResponseWriter, r *http.Request) error {
	if err := saveSettings(s.db, r.PostFormValue); err != nil {
		return err
	}
	writeJSON(w, http.StatusOK, ok)
	return nil
}
