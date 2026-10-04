# YouTube 工作區交接（2026-10-04）

## 目前狀態

- 分支 `feat/youtube-workspace`；**全部變更尚未 commit**（等使用者決定）。
- 規格 `docs/design/youtube-workspace-v1.md`（Q18＝A 已確認）的實作切片 S1–S5 全部完成，
  每片都由主 agent 重跑檢查並審查；逐片驗證紀錄見 `tasks/todo.md`「Implementation plan」。
- 最終檢查（2026-10-04）：`ruff check`、`ruff format --check`、`mypy src` 通過；
  `pytest` 627 passed、1 skipped（另有 Starlette TestClient 的 httpx 棄用警告，與本次無關）。
- 視覺方向已由使用者選定：**簡潔中性**（淺色、系統字體、清楚分欄與表單），記於 `PRODUCT.md`。

## 待做工作

### 1. 真實環境驗收（需要使用者，`tasks/todo.md` 最後一個未勾項目）

使用者須提供／操作：

- [ ] 專案根目錄 `.env` 設定 `GEMINI_API_KEY`（agent 不得讀取或輸出金鑰值）。
- [ ] 確認帳戶可用的模型：翻譯 `gemini-3.5-flash-lite`、問答 `gemini-3.8-flash`
      （候選預設，未實測權限與品質；查證依據 `docs/design/gemini-api-notes.md`）。
- [ ] 一支允許測試的 YouTube 影片（無須登入、非直播）。
- [ ] `uv run vcc serve` 後走完核心流程：載入 → 選畫質／音軌 → 下載 → 播放 → 取得原文字幕 →
      翻譯 → 匯出（MP4／MKV）→ 問答與引用跳播 → 清除預覽 → 刪除整支影片。
- [ ] Chrome 與 Safari 實播、引用跳播、200% 放大、窄視窗「字幕／問答」分頁。
- [ ] VLC 開啟匯出成品，確認字幕軌可切換／關閉，並對照第 5 節匯出矩陣（附加原文開／關）。
- [ ] 螢幕閱讀器（VoiceOver）聽一次 aria-live 進度與聊天宣告。
- [ ] Gemini 回答品質以人工樣本核對語義忠實（AC07 要求，mock 不能代替）。
- [ ] 驗收結果寫回 `tasks/todo.md`；失敗項目開成修正任務。

### 2. Commit 決定

- 使用者尚未要求 commit。詢問要現在 commit 還是驗收後再 commit。
- **不要** add／修改／刪除 untracked 的 `package-lock.json`（使用者既有檔案）。
- `docs/code-review.md` 缺結尾換行是既有問題（1bf3d20），不在本次範圍。

### 3. 需要使用者確認的實作細節（規格未定義，由實作者自選）

| 項目 | 目前做法 |
| --- | --- |
| 工作並行 | 下載、字幕、翻譯、匯出、預覽共用一條串行 lane；問答獨立 lane（主 agent 依規格「原文就緒即可問答」修正） |
| 翻譯分塊 | 每塊 100 cues、預檢上限 900,000 tokens、單次 120 秒 timeout |
| 問答輸入上限 | 常數表：`gemini-3.8-flash` 1,048,576；未知模型 200,000；預算＝min(200,000, 上限−8,192−4,096)；未呼叫 models.get |
| 重複送出問題 | 回 409，不回傳既有 pending 訊息 |
| 手動匯入字幕 | 不自動開對話，須按「設為問答依據」 |
| 原語系未知 | 要求使用者從平台字幕選單手動選軌 |
| 預覽編碼 | libx264 veryfast、crf 23、yuv420p、AAC 128k 雙聲道、faststart；空間估算＝來源×2＋64 MiB |
| 刪除整支影片 | 確認碼＝範圍計數 sha256；等媒體 lane 停下最多 10 秒，逾時回 409 並列入「待清理」；待清理期間同 YouTube ID 不能重新匯入 |
| 容器選擇 | H.264／AAC 用 MP4，其餘保守用 MKV；MP4 失敗須使用者確認改 MKV |
| 「取消查詢」 | 只停止瀏覽器端等待；伺服器已完成的查詢仍會出現在影片庫 |

## 程式位置速查

- 後端：`src/video_content_capture/workspace/`
  - `config.py`（.env／優先序）、`security.py`（Host／Origin／CSP／redaction）
  - `storage.py`（SQLite schema v5、共同發佈 gate `publish_attempt`、刪除流程）
  - `jobs.py`（`MediaQueue`、`JobLanes`）、`youtube.py`（yt-dlp adapter）
  - `subtitles.py`、`translation.py`、`exports.py`、`previews.py`、`qa.py`、`qa_jobs.py`、`app.py`
- 前端：`workspace/static/`（`index.html`、`workspace.css` 的 `:root` token、`workspace.js`、
  `dialog.js`、`narrow-tabs.js`；`workspace.js` 依測試規定不得呼叫 `.focus(`）
- CLI 入口：`vcc serve`（`src/video_content_capture/cli.py`）
- 測試：`tests/test_workspace_*.py`（全部離線，fake yt-dlp／Gemini；小型真實 ffmpeg 合成媒體）

## 驗證命令

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy src \
  && env -u GEMINI_API_KEY -u GOOGLE_API_KEY uv run pytest
```

- pyproject 的 addopts 已含 `-q`，再加 `-q` 會隱藏摘要行。
- 使用者的 shell 環境已設有 Gemini 金鑰環境變數（空目錄啟動仍顯示「已設定」）；跑測試與
  smoke test 時用 `env -u GEMINI_API_KEY -u GOOGLE_API_KEY` 避免任何付費呼叫。

## 工作方式（使用者偏好）

- 實作優先交給 `codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-6.1-sol
  -c model_reasoning_effort=medium`，額度用完才改用 Claude 子代理；主 agent 負責寫任務說明、
  審查 diff、重跑檢查與 smoke test。
- 各切片任務說明（common.md、s1–s5.md）放在上一個 session 的暫存目錄，可能已不存在；
  需要時依本文件與 `tasks/todo.md` 重寫。
- 不得建立含真實金鑰的 `.env`、讀取或輸出金鑰；測試不得下載真實影片或呼叫付費 API。
