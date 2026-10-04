# 暫存計畫：實作 #2、#3、#4（尚未執行）

日期：2026-10-04。狀態：**完成**（三個 PR 已合併至 main，issue 已關閉）。
母 spec：#1。決策：ADR 0004、ADR 0003 修訂、`CONTEXT.md`（燒錄字幕、雙語字幕）。

## 執行方式

- 使用 **Claude Code subagents**，本批**不使用 codex**（使用者 2026-10-04 指示）。
- 三張 ticket 互不 block，可平行：每張一個 subagent，`isolation: worktree`，各自分支。
- 主 agent 負責：寫 slice prompt、等待完成、審 diff、自己重跑驗證、依序整合。

## 分支

- 基底：`docs/burned-in-subtitle-decisions`（218c97d，含 ADR 0004）；先決定是否先合併回 `main`。
- `feat/resolution-only-quality`（#2）
- `feat/burned-in-export`（#3）
- `fix/download-diagnostics-cleanup`（#4）

## 各 ticket 要點（細節見 issue 驗收條件）

### #2 畫質只選解析度
- 查詢回傳實際可用解析度清單與預設（≤1080p 最高；全 >1080p 取最低並明示）。
- 依字幕形式解出 format：燒錄＝最高 FPS／最佳品質不限編碼；字幕軌＝H.264＋AAC 優先。
- 音軌自動選原語言預設軌、唯讀顯示。
- 首次匯入與重新匯出的選單只列解析度。
- 更新 `docs/design/youtube-workspace-v1.md` 第 4 節。
- 測試 seam：工作區 HTTP API（`create_app`＋`TestClient`＋`FakeAdapter`）。

### #3 燒錄字幕成品（單一語系）
- 匯出加字幕形式 `burned`／`tracks`；`tracks` 行為不變。
- 燒錄：MP4、H.264 VideoToolbox＋AAC（AAC 來源 copy），解析度不變；固定樣式、長行換行。
- 進度、取消、磁碟預檢、原子發佈；硬體編碼不可用即明確失敗。
- 成品紀錄快照加字幕形式；DB 遷移，舊成品＝`tracks`。
- 重新匯出面板可選燒錄。更新設計文件第 5 節燒錄敘述。
- 測試 seam：HTTP API＋真實 ffmpeg（無 VideoToolbox 則 skip；以像素差異驗證有字幕）。

### #4 下載可診斷性與暫存清理
- 下載失敗記一行 redacted 錯誤摘要。
- 成功發佈後清 `stage-*.bin`；失敗保留供重試。

## 預期衝突與整合順序

- #2 與 #4 都動 YouTube 下載模組；#2 與 #3 都動匯出流程與前端匯出面板。
- 建議合併順序：**#4 → #2 → #3**（由小到大），主 agent 逐一 rebase 解衝突。

## 每張完成後的驗證（主 agent 親自跑）

- `uv run ruff check`、`uv run ruff format --check`、`MYPYPATH=src uv run mypy`（純 `uv run mypy` 在 main 也會因缺 py.typed 失敗，屬環境問題）、`uv run pytest`
- 審 diff 對照 issue 驗收條件逐項勾選；不符者退回該 subagent 修正。
- 注意：shell 的 `GEMINI_API_KEY` 無效，實機啟動用 `env -u GEMINI_API_KEY -u GOOGLE_API_KEY uv run vcc serve`。

## 完成後

- 在各 issue 留言驗證結果並關閉；開 PR（或依使用者指示合併）。
- 解鎖 #5（雙語燒錄，blocked by #3）與 #6（一鍵流程，blocked by #2、#3）。

## 待使用者決定

- [x] docs 分支先合併回 `main`：已 fast-forward 並 push（main @ 218c97d），docs 分支已刪
- [x] 三張平行執行
- [x] 完成後各開 PR（subagent 只 commit 不 push，由主 agent 驗證後 push 並開 PR）

## 結果（2026-10-04，接手後完成）

- 合併順序與結果（squash merge）：#4 → PR #8（main @ 64e97d2）→ #2 → PR #9（main @ 3a749c9）
  → #3 先 rebase 到 main、改 PR base 後 PR #10（main @ f4a2b93）。無任何衝突。
- 合併前以三個 subagent 分別重驗分支（ruff／format／mypy／pytest 全綠＋逐條核對驗收條件）；
  #10 rebase 後由主 agent 全套重驗。合併後 main：ruff／format／mypy 通過、pytest 666 passed, 1 skipped。
- 實機驗證（merged main，真實影片庫＋ffmpeg＋VideoToolbox，HTTP API 層）：
  - #2：解析度清單去重（1080/720/480/360/240/144）、default 1080、above_1080p=false；
    燒錄解出 399（AV1 60fps）、字幕軌解出 299（H.264+AAC mp4）；audio 唯讀物件。
  - #3：真實燒錄匯出完成（burning 0.16→0.96 → verifying → completed）；成品 MP4 H.264 High
    854×480＋AAC LC；字時段 PSNR 25.3 dB／下三分之一 20.6 vs 空檔 47.4／49.0；CJK 白字黑邊；
    DB v5→v6 遷移實測；tracks 預設行為不變。
  - #4：失敗保留 stage 與 redacted log、成功後無殘留（測試覆蓋，640 passed）。
- 已知限制（各 PR 已載明）：燒錄僅單一語系（雙語＝#5）；10-bit／HDR 不 tone-map；奇數尺寸明確失敗；
  preview 硬體失敗回 409、create 時回 400；commit 後 unlink 的 crash 窗口可能殘留無 DB 列的暫存檔。
- 後續：issue #2／#3／#4 已留言關閉，解鎖 #5 與 #6；三個 worktree 與本地／遠端分支已清理。
