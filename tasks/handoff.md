# YouTube 工作區交接（2026-10-22，PR #31 之後）

## 目前狀態

- `main` 在 `95a8597`（PR #31）。PR #30、#31 都已 squash 合併，工作樹乾淨。
- 檢查：`ruff check`、`ruff format --check`、`mypy src`、完整 `pytest` 都通過。
- `vcc serve`（127.0.0.1:8765，log 在 `/tmp/vcc-serve.log`）已用 `95a8597` 重啟，在背景跑。
  重啟前先 `pgrep -af "bin/vcc serve"` 取 PID 再 `kill`；
  不要用 `pkill -f "vcc serve"`，它會連發出指令的 shell 一起砍掉（exit 144）。
- 影片庫 `outputs/library/` 已遷移到 schema 8（遷移前備份為 `library.sqlite3.backup-77245d01…`）。
  - `f202fe31…`（美股 SPCX…，27:40）：流程重試後匯出完成（720p 燒錄 MP4，1659.8 s）；
    播放字幕由遷移補成 `zh-TW` 譯文，headless Chromium 確認播放器顯示 270 段字幕。
  - `a2358c1e…`（https://youtu.be/A63sedG-p5Q）：燒錄匯出完成，使用者已確認成品正確。

## PR #30、#31 做了什麼

- #30 `subtitles.py` 的 `original_track` 是選原文字幕軌的唯一規則：人工軌，否則自動 `-orig` 軌，
  都沒有就走語音辨識。流程確認畫面也用它。只有翻譯軌的影片不再 429 三次後顯示「來源暫時無法取得」。
  以真實 adapter 對 A63sedG-p5Q 拿掉 `-orig` 軌重現：修改前 `source_busy`，修改後走語音辨識。
- #31 schema 8 一次性遷移：`playback_version_id` 為空的影片補上最新完整譯文，沒有就補最新完整版本。
  之後為空代表使用者關閉字幕或沒有字幕，沒有加「手動關閉」欄位。

## PR #28 做了什麼

https://github.com/huijoson/video_content_capture/pull/28

1. **原文字幕選軌修正**（`workspace/subtitles.py` 的 `acquire_subtitles`）。YouTube 的自動 `en`
   可能是其他語言機器翻譯（該片 `tlang=en&lang=ar`），平台每次回 429；`en-orig` 才是英文語音。
   選軌順序改為人工 → 自動 `-orig` → 其他；版本語系去掉 `-orig` 尾綴。
2. **省操作步驟**：
   - 貼上網址自動查詢（`workspace.js` 的 `youtube-url` input 事件；只查詢不下載）。
   - 第一份完成的字幕自動成為播放字幕，翻譯完成後改用譯文
     （`storage.py` 的 `_adopt_for_playback`，在完成版本的同一交易內執行）。
   - 問答區三顆常用問題按鈕 `#qa-presets`，按一下即送出。
3. **問答提示詞**（`qa.py` 的 `SYSTEM_INSTRUCTION`）：說明時間由系統依 `cue_ids` 標示，
   使用者要求時間時不再列入「缺乏依據」。只實測一次，屬推論有效。

## 已驗證（本 session 實測）

- 問答：送出問題約 20 秒得到驗證通過的回覆；引用「跳至 00:00:10」讓播放器到 10.46 s。
- 貼上網址自動載入、「重點摘要」按鈕送出並回五個有引用的重點（headless Chromium）。
- A63sedG-p5Q 以真實 adapter 取得 1796 段 `en · platform_auto`，末段 1709 s（片長 1721 s），未跑語音辨識；
  重試流程後字幕、翻譯、燒錄匯出全部完成。

## 待做

- 交接時沒有已知待做項目。使用者手動指定翻譯軌（非 `-orig` 的自動軌）時仍會 429，屬已知限制。

## 環境注意

- shell 環境有 `GEMINI_API_KEY`（非空），會蓋過 `.env`；`serve` 啟動時會印警告。目前該金鑰可用。
  若使用者要改用 `.env` 的金鑰，先 `unset GEMINI_API_KEY` 再重啟服務。
- 本機無 JS runtime（deno），yt-dlp 會警告部分格式可能缺失；也沒有 `curl_cffi`（impersonation）。
  目前下載與字幕都能用，僅記錄供 429／缺格式時排查。
- 瀏覽器測試：`~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome` 搭配
  `playwright-core`（本 session 裝在 `/tmp/vccui`，可能已被清除，`npm i playwright-core` 即可）。
- PR 慣例：Conventional Commit 標題、squash 合併；repo 沒有跑測試的 CI，只有 GitGuardian，
  合併前必須在本機跑完整檢查。

## 驗證命令

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy src \
  && env -u GEMINI_API_KEY -u GOOGLE_API_KEY uv run pytest -p no:warnings
```

pyproject 的 addopts 已含 `-q`；要看摘要行用 `uv run pytest -p no:warnings | grep passed`。

## 程式位置速查

- 後端 `src/video_content_capture/workspace/`：`app.py`（路由）、`storage.py`（SQLite、選擇指標、
  `publish_attempt`）、`flows.py`（一鍵流程）、`subtitles.py`（選軌、解析）、`youtube.py`（yt-dlp
  隔離子程序與錯誤分類）、`asr.py`（Gemini／MLX 辨識）、`translation.py`、`qa.py`、`exports.py`、`burned.py`。
- 前端 `workspace/static/`：`index.html`、`workspace.js`、`workspace.css`。
- 名詞與決策：`CONTEXT.md`、`docs/adr/`。
