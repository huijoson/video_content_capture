# 影片內容擷取

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

使用者在自己的電腦開啟 YouTube 影片，保存所需畫質與語言字幕，並透過問答理解
影片內容及相關知識。多人協作與帳號系統未納入已確認範圍。

## Product Purpose

提供本機瀏覽器工作區：輸入一支 YouTube 影片網址，選擇畫質與單一目標字幕語系，
下載附可切換字幕軌的影片；可觀看、切換或匯入字幕，並依字幕與 agent 對話。

## Operating Context

- 既有產品是 Python 3.13 CLI，提供本機 MLX／AssemblyAI 轉錄與 Claude 報告。
- 新增網頁的影片下載及媒體處理在本機執行；字幕翻譯與問答呼叫 Gemini。
- Gemini 憑證由專案根目錄 `.env` 的 `GEMINI_API_KEY` 提供。
- 本機影片庫保存影片、字幕版本與對話，服務重啟後仍可使用，直到手動刪除。

## Capabilities and Constraints

- 每次匯入一支影片，每次選擇一種實際可用畫質與一個目標語系。
- 「附加原文字幕」為獨立匯出選項；關閉時只附目標語系，不刪除原文證據。
- 原語言字幕依序採用 YouTube 人工字幕、自動字幕、本機辨識。
- 可匯入 SRT／VTT，播放字幕與問答依據各自選擇。
- 更換問答依據會建立新對話，舊對話保留當時的字幕版本與引用。
- 影片問答以文字為影片依據，影片主張附時間戳，背景解釋明確標為補充知識。
- 下載成品保留來源影音串流；相容時優先 MP4，否則 MKV。
- 來源無法在瀏覽器播放時另製作相容預覽，保留成品畫質。
- 第一版不包含播放清單批次、字幕時間軸編輯、字幕燒錄、畫面理解或即時網路搜尋。

## Evidence on Hand

- [已確認訪談 Q1–Q18](docs/design/youtube-workspace-interview.md)。
- [領域用語](CONTEXT.md)與[既有 CLI 說明](README.md)。
- [第一版功能規格](docs/design/youtube-workspace-v1.md)：D1–D8 已隨 Q18 確認。
- 網頁實作 S1–S5 已完成並通過離線測試（2026-10-04）；真實 YouTube、Gemini、Safari、VLC 尚待使用者環境驗收，
  驗收前不能宣稱可用於真實影片。

## Product Principles

- 播放選擇、問答依據與下載字幕各有明確用途。
- 已有對話的來源與引用可追溯，字幕修正不覆蓋歷史。
- 保留使用者指定的成品畫質，預覽可另行處理相容性。
- 清楚區分影片內容與模型補充知識。

## Open Decisions

- Q18 已確認（2026-10-04）：規格 D1–D8 作為實作依據；Gemini 模型候選須以真實 key
  驗證權限後定案，查證依據見[Gemini API 查證](docs/design/gemini-api-notes.md)。
- 網頁框架與儲存方案依 D8 採 FastAPI／Uvicorn＋SQLite，實作進行中。
- 視覺風格已選定（2026-10-04）：簡潔中性——淺色背景、系統字體、清楚分欄與表單。
