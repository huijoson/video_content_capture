# Gemini API 查證筆記

查證日期：2026-10-04。範圍：官方文件與官方 SDK 原始碼；未使用真實 key、未呼叫 API。
用途：支援[第一版規格](youtube-workspace-v1.md)第 8 節的設定契約。以下模型分配是
**提案**，仍待 Q18 確認；帳戶權限、品質、實際 quota 與 SDK 相容性皆未測試。

## 模型候選

| 用途 | 候選 stable ID | 官方頁面所列能力 |
| --- | --- | --- |
| 字幕翻譯 | `gemini-3.5-flash-lite` | 文字 structured output；input 1,048,576／output 65,536 tokens |
| 影片問答 | `gemini-3.8-flash` | 文字 structured output；input 1,048,576／output 65,536 tokens |

- 來源：[3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)、
  [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)。
- 不用 `latest` alias，避免版本無聲改變；固定 ID 也不保證使用者帳戶可用。
- 模型 ID 參與翻譯快取識別；失效時明確報錯，不自動換模型。

## SDK 與憑證

- 官方 Python SDK 是 `google-genai`，不是舊的 `google-generativeai`。
  [Libraries](https://ai.google.dev/gemini-api/docs/libraries)
- SDK 自動讀環境變數時 `GOOGLE_API_KEY` 優先於 `GEMINI_API_KEY`。
  後端須自行依產品契約解析 `GEMINI_API_KEY`，再明確傳入 `api_key`。
  [API keys](https://ai.google.dev/gemini-api/docs/api-key)
- `HttpRetryOptions.attempts` 包含第一次請求，0 或 1 代表不重試。規格採手動重試，
  實作須明確設定，不依賴 SDK 預設。
  [SDK types](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/types.py)
- Quota 依 project、model 與 tier 計算，不是每支 key 獨立。
  [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)

## Token 預檢

- 以實際使用的模型呼叫 `count_tokens`，計入來源、問題、對話記憶與 system instruction，
  並保留輸出與安全餘量；不能只以影片分鐘數估算。
- Developer API 的 count 設定不涵蓋所有 generation config，預檢不能宣稱完全精確。
  [Tokens](https://ai.google.dev/gemini-api/docs/tokens)、
  [countTokens](https://ai.google.dev/api/tokens)

## 輸出驗證

- Structured output 只保證格式；schema 與 ID 有效不代表語義忠實，仍需人工樣本驗收。
  [Structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- 必須檢查 finish reason。MAX_TOKENS、安全阻擋、空輸出與無法解析的 JSON 都不得
  發佈為完成版本。[generateContent](https://ai.google.dev/api/generate-content)
- 翻譯時間沿用原片段；問答時間由引用的來源片段 ID 查表，不採信模型輸出的秒數。

## 未驗證項目

- 兩個候選模型在使用者帳戶的權限、翻譯／問答品質、延遲與實際 quota。
- `google-genai` 的安裝版本與 Python 3.13、`uv.lock` 相容性。
- 實際 `count_tokens` 與計費 token 的差距，以及安全餘量的合適大小。
