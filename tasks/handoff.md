# YouTube 工作區交接（2026-10-22）

## 目前狀態

- `main` 在 `ad9330d`（PR #28，已 squash 合併），工作樹乾淨，除了本交接檔與 `tasks/lessons.md`。
- 檢查：`pytest` 800 passed、1 skipped；`ruff check`、`ruff format --check`、`mypy src` 通過。
- 本 session 啟動的 `vcc serve`（127.0.0.1:8765，log 在 `/tmp/vcc-serve.log`）可能仍在背景跑，
  程式碼與 `ad9330d` 相同。重啟前先 `pgrep -af "bin/vcc serve"` 取 PID 再 `kill`；
  不要用 `pkill -f "vcc serve"`，它會連發出指令的 shell 一起砍掉（exit 144）。
- 影片庫 `outputs/library/` 有兩支影片：
  - `f202fe31…`（美股 SPCX…，27:40）：字幕 `zh` 語音辨識＋`zh-TW` 譯文；舊流程停在匯出「已中斷」，
    但已有一份 MKV 字幕軌成品。播放字幕仍是「未選擇」（字幕早於自動採用功能）。
  - `a2358c1e…`（https://youtu.be/A63sedG-p5Q，28:41，英文）：`en` 平台自動字幕＋`zh-TW` 譯文，
    燒錄匯出完成，使用者已確認成品正確。

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

## 待做（依優先序）

1. **只有翻譯軌、沒有 `-orig` 的影片**仍會 429 三次後顯示 `source_busy`「來源暫時無法取得」，
   誤導使用者重試。候選做法：字幕下載的 429 不再當暫時性錯誤重試，改走 Gemini 語音辨識，
   或回報專屬錯誤碼。`youtube.py` 的 `_HELPER` `reason()` 目前把 429 一律歸為 `temporary`。
2. **既有影片的播放字幕**：自動採用只在新版本完成時觸發，`f202fe31…` 仍需手動按「用於播放」。
   可考慮在開啟影片時，若 `playback_version_id` 為空且使用者從未手動關閉，就補選；
   但目前 schema 無法區分「從未選」與「手動關閉」，需加欄位才做得乾淨。
3. 舊影片 `f202fe31…` 的流程停在匯出「已中斷」，是否重試由使用者決定（會重新匯出）。

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
