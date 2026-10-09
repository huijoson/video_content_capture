# 影片內容擷取

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

使用者在自己的電腦開啟 YouTube 影片，保存所需畫質與語言字幕，並透過問答理解
影片內容及相關知識。多人協作與帳號系統未納入已確認範圍。

## Product Purpose

提供本機瀏覽器工作區：輸入一支 YouTube 影片網址，選擇解析度、單一目標字幕語系
與字幕形式，下載附字幕的影片；可觀看、切換或匯入字幕，並依字幕與 agent 對話。

## Operating Context

- 既有產品是 Python 3.13 CLI，提供本機 MLX／AssemblyAI 轉錄與 Claude 報告。
- 新增網頁的影片下載及媒體處理在本機執行；字幕翻譯與問答呼叫 Gemini。
- Gemini 憑證由專案根目錄 `.env` 的 `GEMINI_API_KEY` 提供。
- 本機影片庫保存影片、字幕版本與對話，服務重啟後仍可使用，直到手動刪除。

## Capabilities and Constraints

- 每次匯入一支影片，每次選擇一種實際可用解析度與一個目標語系。
- 畫質只選解析度：列出實際來源高度、不放大，預設為不高於 1080p 的最高解析度；
  全部高於 1080p 時預選其中最低並明示（ADR 0004）。
- 字幕形式可選**可切換字幕軌**或**燒錄字幕**，預設燒錄（ADR 0004，推翻訪談 Q3）。
- 燒錄須重新編碼，一律輸出 MP4（H.264＋AAC），維持所選解析度，使用 Mac 硬體編碼
  （VideoToolbox）；改字幕需重新輸出整支影片。
- 燒錄可選雙語字幕（目標語系在上、原文在下），樣式固定不提供調整。
- 「附加原文字幕」為獨立匯出選項；關閉時只附目標語系，不刪除原文證據。
- 原語言字幕依序採用 YouTube 人工字幕、自動字幕、本機辨識。
- 可匯入 SRT／VTT，播放字幕與問答依據各自選擇。
- 更換問答依據會建立新對話，舊對話保留當時的字幕版本與引用。
- 影片問答以文字為影片依據，影片主張附時間戳，背景解釋明確標為補充知識。
- 可切換字幕軌成品保留來源影音串流；相容時優先 MP4，否則 MKV。
  燒錄成品不適用（ADR 0003 僅涵蓋字幕軌成品）。
- 來源無法在瀏覽器播放時另製作相容預覽，保留成品畫質。
- 第一版不包含播放清單批次、字幕時間軸編輯、畫面理解或即時網路搜尋。

## Brand Commitments

- 視覺方向（2026-10-08）：字幕校對格——重製現場的一張校對表。桌面紙底與紙白工作表、
  1px 校對線、等寬時間碼與尺寸、墨黑實心主要動作。in/out 紅標出破壞、失敗、播放中的
  IN／OUT 邊界，以及版本暫存器「目前做到哪」的那一道記號；版本綠只標完成。
  取代 2026-10-04 的「簡潔中性」。

## Evidence on Hand

- [已確認訪談 Q1–Q18](docs/design/youtube-workspace-interview.md)，其中 Q3 已由
  [ADR 0004](docs/adr/0004-burned-in-subtitle-export.md) 推翻；[ADR 0001–0004](docs/adr/)。
- [領域用語](CONTEXT.md)與[既有 CLI 說明](README.md)。
- [第一版功能規格](docs/design/youtube-workspace-v1.md)：D1–D8 已隨 Q18 確認。
- 網頁工作區已實作並陸續修正：一鍵流程、燒錄與雙語字幕、字幕取得與 Gemini 翻譯
  邊界問題（GitHub issues #1–#17 全數關閉）。
- 離線測試 36 個檔案；真實環境驗收為選擇性 `-m live`（需 `VCC_ENABLE_LIVE=1`
  與憑證），預設測試套件全離線。
- 真實 YouTube、Gemini、Safari 與 VLC 的平台相容組合仍待逐項驗收；
  [媒體相容性查證](docs/design/media-compatibility-notes.md)僅為可能性結果，不能替代播放驗收。

## Product Principles

- 播放選擇、問答依據與下載字幕各有明確用途。
- 已有對話的來源與引用可追溯，字幕修正不覆蓋歷史。
- 保留使用者指定的成品畫質，預覽可另行處理相容性。
- 清楚區分影片內容與模型補充知識。

## Open Decisions

- Gemini 模型候選（`gemini-3.5-flash-lite` 翻譯／`gemini-3.8-flash` 問答）須以真實 key
  驗證權限、品質、延遲與 quota 後定案，查證依據見
  [Gemini API 查證](docs/design/gemini-api-notes.md)。
