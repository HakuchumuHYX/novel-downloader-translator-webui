package app

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/HakuchumuHYX/novel-downloader-translator-webui/downloader"
	"github.com/HakuchumuHYX/novel-downloader-translator-webui/translator"
)

// 单 goroutine 任务队列：按顺序跑下载 / 翻译，进度和日志直接写库。
// 停止任务 = 取消当前任务的 ctx；任务返回后库里状态不是 running 就当作已取消，不覆盖状态。

const logKeep = 2000

var errCanceled = errors.New("任务已取消")

type worker struct {
	db      *sql.DB
	taskDir string
	wake    chan struct{}

	mu      sync.Mutex
	current int64 // 正在跑的任务 id，0 表示空闲
	cancel  context.CancelFunc

	logCount atomic.Int64 // 翻译并发时多个 goroutine 同时写日志
}

func newWorker(db *sql.DB, taskDir string) *worker {
	return &worker{db: db, taskDir: taskDir, wake: make(chan struct{}, 1)}
}

func (w *worker) notify() {
	select {
	case w.wake <- struct{}{}:
	default:
	}
}

func (w *worker) stopTask(id int64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.current == id {
		w.cancel()
	}
}

func (w *worker) run(ctx context.Context) {
	for {
		task, err := w.claimNext()
		if err != nil {
			log.Printf("领取任务失败：%v", err)
		}
		if task == nil {
			select {
			case <-ctx.Done():
				return
			case <-w.wake:
			}
			continue
		}
		w.handle(ctx, task)
	}
}

func (w *worker) claimNext() (*Task, error) {
	tx, err := w.db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	task, err := scanTask(tx.QueryRow("SELECT " + taskColumns + " FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1"))
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec("UPDATE tasks SET status = 'running', started_at = ?, finished_at = NULL, error = '', "+
		"stage = '', progress_current = 0, progress_total = 0 WHERE id = ?", now(), task.ID)
	if err != nil {
		return nil, err
	}
	return &task, tx.Commit()
}

func (w *worker) handle(ctx context.Context, task *Task) {
	taskCtx, cancel := context.WithCancel(ctx)
	w.mu.Lock()
	w.current, w.cancel = task.ID, cancel
	w.mu.Unlock()
	defer func() {
		w.mu.Lock()
		w.current, w.cancel = 0, nil
		w.mu.Unlock()
		cancel()
	}()

	err := w.process(taskCtx, task)
	if ctx.Err() != nil {
		return // 服务正在退出：保持 running，下次启动时统一标为中断
	}
	status, statusErr := taskStatus(w.db, task.ID)
	if statusErr != nil {
		log.Printf("任务 %d 查询状态失败：%v", task.ID, statusErr)
		return
	}
	if status != "running" {
		return // 已被停止/删除接口改掉
	}
	if err != nil {
		w.log(task.ID, "失败："+err.Error())
		w.finish(task.ID, "failed", err.Error())
	} else {
		w.finish(task.ID, "succeeded", "")
	}
}

func (w *worker) finish(id int64, status, message string) {
	// 只收尾仍在 running 的任务：被停止的任务已由接口改成 canceled
	_, err := w.db.Exec("UPDATE tasks SET status = ?, error = ?, finished_at = ? WHERE id = ? AND status = 'running'",
		status, message, now(), id)
	if err != nil {
		log.Printf("任务 %d 收尾失败：%v", id, err)
	}
}

func (w *worker) log(id int64, message string) {
	if err := addLog(w.db, id, message); err != nil {
		log.Printf("任务 %d 写日志失败：%v", id, err)
		return
	}
	if w.logCount.Add(1)%200 == 0 {
		_, err := w.db.Exec("DELETE FROM task_logs WHERE task_id = ? AND id <= "+
			"(SELECT id FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)", id, id, logKeep)
		if err != nil {
			log.Printf("任务 %d 清理日志失败：%v", id, err)
		}
	}
}

// startStage 进入新阶段前确认任务没被停止：停止请求可能恰好落在两个阶段之间
func (w *worker) startStage(id int64, stage string) error {
	status, err := taskStatus(w.db, id)
	if err != nil {
		return err
	}
	if status != "running" {
		return errCanceled
	}
	_, err = w.db.Exec("UPDATE tasks SET stage = ?, progress_current = 0, progress_total = 0 WHERE id = ?", stage, id)
	return err
}

func (w *worker) process(ctx context.Context, task *Task) (err error) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("任务 %d panic：%v\n%s", task.ID, r, debug.Stack())
			err = fmt.Errorf("内部错误：%v", r)
		}
	}()

	cfg, err := loadSettings(w.db)
	if err != nil {
		return err
	}
	taskDir := filepath.Join(w.taskDir, strconv.FormatInt(task.ID, 10))
	work, out := filepath.Join(taskDir, "work"), filepath.Join(taskDir, "out")
	logf := func(format string, args ...any) {
		w.log(task.ID, fmt.Sprintf(format, args...))
	}
	progress := func(current, total int) {
		_, err := w.db.Exec("UPDATE tasks SET progress_current = ?, progress_total = ? WHERE id = ?", current, total, task.ID)
		if err != nil {
			log.Printf("任务 %d 更新进度失败：%v", task.ID, err)
		}
	}

	var source string
	if task.URL != "" {
		if err := w.startStage(task.ID, "download"); err != nil {
			return err
		}
		site, err := downloader.DetectSite(task.URL)
		if err != nil {
			return err
		}
		downloadOnly := task.Options.Mode == "download"
		opts := downloader.Options{
			URL:     task.URL,
			WorkDir: work,
			OutDir:  work,
			Proxy:   cfg["proxy"],
			Cookie:  cfg["cookie_"+site],
			Epub:    downloadOnly && task.Options.Format == "epub",
		}
		if downloadOnly {
			opts.OutDir = out
		}
		source, err = downloader.Download(ctx, opts, logf, progress)
		if err != nil {
			return fmt.Errorf("下载失败：%w", err)
		}
		title := strings.TrimSuffix(filepath.Base(source), filepath.Ext(source))
		if _, err := w.db.Exec("UPDATE tasks SET title = ? WHERE id = ?", title, task.ID); err != nil {
			return err
		}
	} else {
		source = filepath.Join(taskDir, "input", task.UploadName)
	}

	if task.Options.Mode != "translate" {
		return nil
	}
	if err := w.startStage(task.ID, "translate"); err != nil {
		return err
	}
	workers, _ := strconv.Atoi(cfg["parallel_workers"])
	translatorCfg := translator.Config{
		APIBase:      cfg["api_base"],
		APIKey:       cfg["api_key"],
		Model:        cfg["model"],
		Language:     cfg["language"],
		PromptSystem: cfg["prompt_system"],
		PromptUser:   cfg["prompt_user"],
		Workers:      workers,
		Proxy:        cfg["proxy"],
	}
	translatorOpts := translator.Options{
		Epub:       task.Options.Format == "epub",
		Bilingual:  task.Options.Bilingual,
		TestBlocks: task.Options.TestBlocks,
	}
	if _, err := translator.Translate(ctx, translatorCfg, source, work, out, translatorOpts, logf, progress); err != nil {
		return fmt.Errorf("翻译失败：%w", err)
	}
	return nil
}
