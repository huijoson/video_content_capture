# 暫存計畫：實作 #5、#6

日期：2026-10-04／05。狀態：**進行中**（#5 已合併，#6 驗證完成待合併）。
母 spec：#1。前一批：#2／#3／#4 見 `plan-issues-2-4.md`。

## 執行方式

- 沿用上一批的分工：每張 ticket 一個 subagent、`isolation: worktree`、各自分支；主 agent
  負責寫 slice prompt、審 diff、自己重跑驗證、依序整合。
- 本批額外要求：實作完成後由**獨立** subagent 重驗（只看證據，不看實作說明），發現問題
  退回原 subagent 修正，直到判定 SHIP。

## 分支

- `feat/bilingual-burned`（#5）→ PR #11 → 已 squash 合併（main @ `9467a98`）
- `feat/one-click-flow`（#6）→ PR #12 → 待審

## #5 雙語燒錄字幕

- 目標語系在上、原文在下、字級較小，各自依自己的 cue 起訖顯示。
- 關鍵設計轉折：初版以字型量測推導保留區高度，被獨立驗證以像素證明**會在真實字幕下反轉
  順序**（libass 碰撞迴避把 2 行以上的原文抬到目標之上）。改為**結構性保證**——兩份字幕
  使用不同 ASS 圖層（目標 layer 1、原文 layer 0），libass 只在同層內避讓。
- 另加保留區上限 `margin_limit = max(0, min(round(height × 0.5), height − font_size))`，
  確保目標永不出框。
- 單語路徑位元組不變（3000 例 fuzz、0 mismatches）。
- 已知非阻斷限制：超過上限時原文向上溢出（目標不位移、不被覆蓋）；`LINE_SPACING` 不再是
  通用上界（實測 Tamil 1.358 em、Khmer 1.474 em），但順序已不依賴它。

## #6 一鍵流程

- 確認畫面（解析度、目標語系、原文＋目標、字幕形式預設燒錄）＋唯讀事實＋預計步驟；
  按「開始處理」凍結快照，之後改選不影響進行中的流程。
- 後端：schema v7（`flows` 表、`flows_one_running` 部分唯一索引、`jobs.flow_id`）；
  `flows.py` 協調者在每個階段成功後建立**既有**工作種類的下一階段，不重新實作。
- 端點：`GET /api/videos/{id}/flow`、`POST /api/videos/{id}/flows`、`GET /api/flows/{id}`。
- `BILINGUAL_BURNED` 是對匯出契約的執行期探測。它讓 #6 不必硬相依 #5：在 #5 合併前為
  `False`，此時「燒錄＋原文」以明確訊息拒絕而非產生錯誤成品；#5 合併後自動翻為 `True`，
  本分支無需改動。

## 已知既有缺陷（另案追蹤）

- **#13**：YouTube 自動字幕 VTT 的標頭 metadata 區塊（`Kind:`／`Language:`）被當成第一條
  字幕，導致 `subtitle_acquisition_failed`。與 #5／#6 無關，`main` 亦重現。發現於 #6 的
  實機端到端驗證。一鍵流程在此情況下正確停在字幕階段、不推進。

## 待使用者決定

- [ ] #5 的兩個已知非阻斷限制是否要現在處理（超上限溢出、極小尺寸退化影格）。
- [x] 是否修 #13：使用者決定先不處理，維持已開 issue。

## 結果（2026-10-05）

### #5（PR #11，main @ `9467a98`）

- 四關全綠；`pytest` 675 passed, 1 skipped（6 個真實 ffmpeg 測試實際執行、非 skip）。
- 兩機制皆以「強制回復舊語意」證明非空洞：移除 layer 使 2 測試失敗、移除上限使 3 測試失敗。
- 獨立驗證者多輪以像素為證據，最終 **SHIP WITH CAVEATS**；過程中它更正了自己先前的量測錯誤，
  實作者亦否證了驗證者一度提出的 1.42 em 行距數字為其探針假影。

### #6（PR #12，head `5bb0e9e`）

- rebase 到含 #5 的 main 後，`BILINGUAL_BURNED` 自動翻為 `True`（無程式改動），
  `flows.py:237` 的拒絕分支成為安全網（強制設 `False` 時仍會觸發）。
- 四關全綠；`pytest` **686 passed, 1 skipped**。
- **實機端到端**（真實伺服器、真實 YouTube 來源、真實 ffmpeg／VideoToolbox、真實 Gemini
  翻譯）：流程 `480c41b1961c46b9a2613a12c79a425e` 四階段全部完成，
  `subtitle_form=burned` ＋ `include_original=true`；成品 `f2eecb81c8dd4259b30ac4e5f46d5bb0`
  HTTP 200、163 MB、H.264 854×480 + AAC。
- 逐像素確認雙語確實上畫面：目標（zh-TW）亮帶 y 385–406 在原文（en）y 426–455 **上方**；
  三張影格 md5 互異，排除 seek 假影格。
- **遷移實測**：真實 v6 影片庫複本 → v7，`user_version` 6→7、新增 `flows` 表，
  原影片／assets／jobs 全數保留。反向亦確認：main（v6）開 v7 庫會明確拒絕，不靜默毀損。
- 失敗路徑亦實測：來源不可用時流程 `failed/source_unavailable`，後續階段維持 `pending`
  不推進；撞到 #13 時停在字幕階段。

### 獨立驗證與修正（head `0b8b1f8`）

獨立 subagent 重驗 PR #12（只讀證據、不看實作說明），8 項驗收全數 PASS，另提 5 項發現：

| 編號 | 嚴重度 | 內容 | 處置 |
| --- | --- | --- | --- |
| D1 | 阻斷 | 崩潰後重啟，`create_job` 去重時沿用舊的下載工作但**不套用傳入的 `flow_id`**，流程永遠停在 `running` | 已修（`eab6949`） |
| D2 | 阻斷 | 在**佇列中**取消流程階段時，工作被移出 `pending`，worker 從未執行、完成回呼從未觸發，流程同樣卡死 | 已修（`0b8b1f8`） |
| D3 | 非阻斷 | `bilingual_burned` 有回傳但前端未讀 | 無需處理（`BILINGUAL_BURNED` 已是 `True`） |
| D4 | 非阻斷 | 目標語系選單的空選項「不翻譯」被後端偷偷替換成原文語系 | 已修（`0b8b1f8`） |
| D5 | 資訊 | `frozen=True` 與 `flows.py` 的「燒錄＋雙語」守衛未被測試覆蓋 | 未處理（未要求） |

**D1 機制**：`flows.py` 呼叫 `create_job(video_id, format_id, audio_id, flow_id)`，但去重分支
只回傳既有列就結束。崩潰（或 `interrupt_running()`，它只碰 `running`）會留下 `queued` 且
`flow_id = NULL` 的下載工作；下一個流程沿用該工作，於是 `on_job_finished` 看到
`flow_id is None` 直接 return，流程永遠 `running`，之後每次開始都 409，且沒有任何端點能結束它。
修法：沿用工作時一併 `UPDATE jobs SET flow_id = ?` 並回傳更新後的列；`flow_id is None` 的呼叫者
行為不變。回歸測試 `test_flow_reclaims_a_queued_download_left_unlinked_by_a_crash`。

**D2 機制**：`workspace.js` 對所有 `queued`／`running` 工作都畫出「取消」按鈕，包含流程階段。
`MediaQueue.cancel` 把仍在 `pending` 的工作直接移除，於是 worker 永遠不會執行它，`_worker`
`finally` 裡的完成回呼（流程串接所依賴）也永遠不會觸發。儲存層探針原樣重現
（`STAGE: queued → AFTER CANCEL: cancelled → FLOW: running → WEDGED`）。
修法：`cancel` 在移除前先記下它是否還在佇列，若是就自行呼叫抽出的 `_finished()`；
`flows.advance()` 把 `cancelled` 對應到 `update_flow(status="cancelled", error="cancelled")`，
而不是誤記為失敗。`signal()`（清理交易用）刻意不觸發回呼——`purge_video` 直接刪除 `flows` 列，
`clear_previews` 只取消 `kind='preview'`，兩者都不會產生卡死的流程。

**D4 機制**：選單原本以空值「不翻譯（只處理原文字幕）」開頭，但後端無法表達「不翻譯」——
`FlowChoice.target_language` 是必填、`start()` 無條件解析它、`app.py` 的 `FlowRequest` 缺欄位
直接 422，`GET /flow?target_language=`（空字串）回 400。原前端於是以
`value || original_language || "zh-TW"` 偷偷代入使用者沒選的語系。issue #6 的驗收只要求
「目標語系（一次一個）」，故正解是**移除該選項**：選單改由 `zh-TW` 起算、切換影片時一併重設，
送出時原樣帶上選取値。

**非空洞證明**：把 D2 的後端修正 `git stash` 後，新增的三個測試（佇列層、清理 `signal`
負向、流程層）中對應的兩個立刻失敗。

修正後四關全綠：`pytest` **692 passed, 1 skipped**。
