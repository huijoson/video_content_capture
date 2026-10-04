# 2026-10-04 S5 相容預覽、清理、整體 UI 與可及性

## Goal and acceptance criteria

- AC06 預覽：可直接播放不建預覽；不可播放時手動製作最高 720p H.264／AAC faststart MP4，獨立
  previews 位置原子發佈，不覆寫來源／成品；可取消（終止子程序）／重試；失敗保留字幕、問答、成品。
- D5 清理：清除預覽只動預覽；整支刪除先列範圍＋範圍綁定確認碼，deleting → 停工作 → 刪檔 → 刪列，
  失敗列為待清理可重試；刪除後同 YouTube ID 可重新匯入，舊回覆不寫入（HTTP 全流程）。
- AC12：三欄 UI（簡潔中性）、CSS token 集中、深色模式、文字＋圖示狀態、可見標籤、鍵盤對話框、
  aria-live、375px／200% 單欄；既有 CLI 回歸。

## Plan

- [x] A：讀取 S5 prompt、規格 3／4／7／D4／D5、媒體查證、lessons 與既有程式／測試。
- [x] B：先寫失敗測試（previews、cleanup、s5_api、s5_ui）→ schema v5、previews.py、清理 API、頁面。
- [x] C：真實 ffmpeg（VP9/Opus、ffv1 1080p）＋ ffprobe 驗證；headless Chrome（CDP）實測。
- [x] Verify：uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
- [x] D：README `vcc serve` 說明、結果與待真實環境驗收項目。

## Risk & rollback

- 風險中：schema v5（media_previews）與不可逆刪除；升級前沿用 SQLite backup，刪除不跟隨符號連結、
  只處理受控 ID 目錄。回復：停止服務、保留影片庫與備份，回到先前程式；不讓舊程式讀 v5。

## Results

- 離線驗證：ruff／format／mypy 通過；pytest 626 passed、1 skipped，全部 fake provider、無下載。
- 對話刪除改用 queue.signal：原 queue.cancel 在 DB 已取消時拋錯，從未設定 worker 取消事件。
- 測試調整：tests/test_workspace_api.py 原檢查「下一階段提供」占位文字，S5 實作後改為「製作相容預覽」。
- Chrome headless 實測：VP9/Opus 來源 → 預覽實際解碼 640x360、鍵盤開啟刪除對話框／Tab 限制／Esc 關閉
  ／焦點歸還、輸入框空白鍵、375px 與 640px（200% 等效）無橫向捲動、深色 token、刪除全流程。
- 待真實環境：Safari 實播、VLC 成品切軌、螢幕閱讀器宣告、真實 YouTube／Gemini。

---

# 2026-10-04 S4 影片問答、來源版本與可信引用

## Goal and acceptance criteria

- AC07：僅完整當時來源的片段可引用，時間／原文由保存版本映射，整份驗證後顯示。
- AC08：固定對話／來源／訊息／attempt，取消、重試、刪除與切換不發佈遲到結果。
- AC11：完整來源＋system＋最近最多 8 組成功問答＋問題計數；先減舊記憶，仍超限零生成。
- AC09／AC10 問答部分：重啟 pending 失敗不重送、缺 key 零呼叫、金鑰不持久化。

## Plan

- [x] A：讀取規格、ADR、領域、lessons、S3 gate／Gemini／頁面模式。
- [x] B：逐項失敗測試 → schema v4、問答邊界、工作／API、頁面最小實作。
- [x] C：離線 fake 驗證來源／記憶／token、四種競態、重啟／並發／金鑰。
- [x] Verify：uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
- [x] D：審查 diff、更新結果與待真實環境驗收項目。

## Risk & rollback

- 風險中：SQLite schema 升級與雲端回覆持久化；升級前沿用 SQLite backup。
- 回復：停止服務，保留完整影片庫；恢復升級前備份與對應程式版本，不讓舊程式讀 v4。
- 不 commit／push／切分支；既有 CLI 與 S1–S3 工作樹保留。

## Dependencies & environment

- Python 3.13、現有 google-genai／FastAPI；本切片預期無新增依賴。
- 測試只用 fixture／fake／monkeypatch，不讀真實 key、不下載、不付費。

## Working notes

- 所有處理沿用單一 MediaQueue，Gemini 與媒體工作串行；不加入翻譯塊搶占。
- 共同 publish_attempt gate 加上對話／來源／訊息 owner validator。
- 使用者已指定測試 storage、fake Gemini、API／頁面邊界。

## Results

- 離線驗證：ruff／format／mypy 通過；pytest 587 passed、1 skipped（全部 fake Gemini、無下載）。
- 補測：API 層取消→重試只回覆一次、token 超限失敗可重試且不入記憶、跨影片對話／版本拒絕、
  影片清除後同 ID 重新匯入不收舊回覆、問題／記憶 prompt injection 只在資料區、引用跳播用當時版本。
- 修正：刪除對話後的 tombstone 不再以 FK 阻擋字幕版本永久刪除。
- 待真實環境：Gemini 模型權限與 structured output 實際品質、瀏覽器跳播與 aria-live 宣告。

---

# 2026-10-03 YouTube 介面、字幕與影片問答設計訪談

## Goal and acceptance criteria

- [x] 記錄 YouTube 網址、畫質下載、所選語系字幕、Gemini 設定檔金鑰、影片問答與更換字幕需求。
- [x] 確認運行型態：本機瀏覽器介面，影片下載與處理在本機執行。
- [x] 確認字幕更換範圍：播放時切換語言、匯入 SRT／VTT 字幕。
- [x] 確認下載成品使用可切換字幕軌。
- [x] 確認字幕來源順序：YouTube 原語言人工字幕 → 自動字幕 → 本機辨識。
- [x] 確認問答知識範圍：影片內容附時間戳，背景解釋標示為補充知識。
- [x] 確認翻譯與問答共用設定檔中的 Gemini API key。
- [x] 確認播放字幕與問答依據分開選擇，匯入修正版可明確設為問答依據。
- [x] 確認更換問答依據另開新對話，保留舊對話與其字幕版本。
- [x] 確認第一版以字幕／逐字稿問答並提供背景解釋，不加入畫面理解。
- [x] 確認第一版每次匯入單支 YouTube 影片。
- [x] 確認列出來源實際可用畫質，每次選一種下載。
- [x] 依使用者修正，將「附加原文字幕」設為獨立開關；關閉時只匯出選定語系。
- [x] 確認目標字幕語系每次單選，原文開關獨立控制。
- [x] 確認必要時另製作本機相容預覽，下載成品保留所選畫質。
- [x] 確認成品保留來源影音串流，相容時優先 MP4，否則使用 MKV。
- [x] 確認自動保存本機影片庫，跨次保留影片、字幕版本與對話，直到手動刪除。
- [x] 確認 Gemini 金鑰來自專案根目錄 `.env` 的 `GEMINI_API_KEY`（Q17，2026-10-04）。
- [x] 釐清輸出相容性與版本處理、問答依據及各分支的行為與失敗情境（v1 規格＋review 修訂）。
- [x] 建立可驗收的設計：`docs/design/youtube-workspace-v1.md`（D1–D8 仍為提案）。
- [x] Q18 已確認 A（2026-10-04）：規格 D1–D8／AC01–AC12 為實作依據，並授權實作。
- [ ] 持續維護領域詞彙；僅對符合條件的已確認決策建立 ADR。

## Plan

- [x] 閱讀 grill-with-docs、grilling、domain-modeling 與現有 lessons。
- [x] 檢查 README、設定與領域模型，委派唯讀程式探索。
- [x] 從使用情境開始逐輪確認設計，Q1–Q17 已確認並記錄。
- [x] 將已確認設計轉為介面流程與具體驗收情境（v1 規格）。
- [x] 2026-10-04 Claude Code 接手：套用六項唯讀 review 發現（見 Results）。
- [x] 補 Gemini 官方查證筆記 `docs/design/gemini-api-notes.md`，模型與記憶政策標為提案。
- [x] 清除 PRODUCT.md 與 v1 規格的簡體字漏入。
- [x] Verify：需求覆蓋、文件連結、行尾空白／換行及 `git diff --check`。
- [x] 向使用者提出 Q18 整體確認，使用者回覆 A。
- [x] 建立分支 `feat/youtube-workspace`；基準 `uv run pytest -q`、ruff、mypy 全通過。

## Implementation plan（Q18 後，2026-10-04）

執行方式：使用者指定由 subagent 實作，優先 `codex exec --dangerously-bypass-approvals-and-sandbox
-m gpt-6.1-sol -c model_reasoning_effort=medium`；codex 額度用完再改用 Claude Code subagent。
主 agent 每片負責審查 diff、重跑驗證並更新本清單。不提交 commit，除非使用者要求。

- [x] S1 本機服務、設定、影片庫：`vcc serve`、loopback／Host／Origin 防護、`.env` 載入與優先序、
  `GEMINI_API_KEY` SecretStr、SQLite schema（版本號）、影片庫目錄、空狀態頁面；AC10 設定部分、AC09 重啟持久化。
  - S1 驗證（主 agent 2026-10-04）：ruff／format／mypy 通過，pytest 410 passed、1 skipped；實際 serve：
    status 不含金鑰、錯誤 Host 400、跨站 POST 403、0.0.0.0 被拒、日誌無測試金鑰。
- [x] S2 YouTube 查詢／下載／播放：URL 驗證、yt-dlp metadata、實際畫質／音軌、工作佇列（attempt ID、
  取消、中斷、共同發佈條件）、stream copy 播放；AC01、AC08 工作競態部分、AC09。
  - S2 驗證（主 agent 2026-10-04）：ruff／format／mypy 通過，pytest 464 passed、1 skipped；審查共同發佈
    gate（BEGIN IMMEDIATE 內核對 attempt／running／deleting）與子程序 killpg；實際 serve：非 YouTube 網址
    回「來源不支援」、跨站 POST 403、路徑穿越 404。真實 YouTube／瀏覽器實播未驗。
- [x] S3 字幕：平台字幕取得順序、SRT／VTT 匯入驗證、不可變版本、Gemini 翻譯（顯式 key、無自動重試、
  finish reason、分塊 ID 驗證）、「以此版本翻譯」、匯出快照與 MP4／MKV 封裝；AC02–AC06。
  - S3 驗證（主 agent 2026-10-04）：ruff／format／mypy 通過，pytest 535 passed、1 skipped；審查翻譯：顯式
    key、attempts=1、count_tokens、finish reason 非 STOP 即失敗、階段續接、經共同 gate 發佈；無 key 啟動
    status configured=false、首頁 200。真實 Gemini／MLX／VLC 未驗。
- [x] S4 問答：對話固定來源版本、count_tokens 預檢、最近 8 組記憶、引用驗證與跳播、遲到結果；AC07、AC08、AC11。
  - S4 實作（接手 agent 2026-10-04，待主 agent 審查）：schema v4（conversations／qa_messages、v3 備份升級）、
    structured output 三類回覆＋後端由保存版本計算引用、count_tokens 預檢由舊到新減記憶／仍超限零生成、
    最近 8 組成功問答記憶、共同 gate＋owner validator、四種競態、重啟不重送、同對話 409、對話區頁面；
    修正已刪對話 tombstone 阻擋字幕版本刪除（FK）。ruff／format／mypy 通過，pytest 587 passed、1 skipped。
    真實 Gemini／瀏覽器實際跳播未驗。
  - S4 驗證（主 agent 2026-10-04）：codex 額度用盡後由 Claude 子代理收尾；主 agent 審查並修正規格偏差——
    問答原與下載／匯出共用單一佇列，違反「原文就緒即可問答」；改為 QA 獨立 lane（JobLanes），新增儲存層與
    API 測試（無修正時失敗）。ruff／format／mypy 通過，pytest 589 passed、1 skipped；實際 serve 首頁 200、
    不存在對話 404、跨站 403。真實 Gemini 問答品質未驗。
- [x] S5 相容預覽、清理、整體 UI 與可及性；AC06 預覽、AC12；既有 CLI 回歸。視覺方向於此片前確認。
  - S5 驗證（主 agent 2026-10-04）：Claude 子代理實作；主 agent 補上規格第 3 節窄視窗「字幕／問答」分頁與
    影片庫收合（narrow-tabs.js，方向鍵／Home／End、roving tabindex、寬版移除 tabpanel role），新增 UI 測試。
    ruff／format／mypy 通過，pytest 627 passed、1 skipped；headless Chrome 375px：播放器在分頁上方、切換保留
    草稿、方向鍵切到問答並移焦點、無橫向捲動、無 JS 錯誤；1400px 分頁隱藏、三欄同時顯示。
- [ ] 真實環境驗收（需使用者提供 `.env` 金鑰與允許的測試影片）：Gemini 模型權限、Chrome／Safari、VLC。

## Working notes

- 訪談紀錄：`docs/design/youtube-workspace-interview.md`；詞彙：`CONTEXT.md`。
- `grilling` 要求使用者確認共享理解後才開始實作；本階段為設計訪談與文件。
- 現有產品是 Python CLI，設定目前由環境變數載入，尚無 Gemini 設定欄位。
- Q1 已確認本機網頁，已記錄 `docs/adr/0001-local-browser-interface.md`；Gemini 翻譯仍使用雲端。
- Q2 已確認 A + B：切換語系、匯入字幕；介面內字幕編輯不納入本次範圍。
- Q3 已確認 A：下載成品附可切換字幕軌；容器、播放器、軌數與獨立字幕匯出待確認。
- Q4 已確認 A：優先既有字幕，無字幕才本機辨識，按需使用 Gemini 翻譯。
- Q5 已確認 B：影片內容加背景解釋、區分引用與補充知識，本次不加入即時網路查詢。
- Q6 已確認 A：翻譯與問答共用 Gemini 金鑰；設定檔格式、位置與模型名稱仍待決定。
- Q7 已確認 A：播放字幕與問答依據分開，記錄於 `docs/adr/0002-separate-playback-and-qa-source.md`。
- Q8 已確認 A：更換依據開新對話，舊對話保留原字幕版本與引用，已補入 ADR 0002。
- Q9 已確認 A：問答以文字為影片依據；不能把字幕未提供的畫面資訊當成影片事實。
- Q10 已確認 A：單支影片匯入，本版不加入多網址批次或播放清單匯入。
- Q11 已確認 A：每次下載一種實際可用畫質。
- Q12 已確認獨立原文開關；Q13 已確認 A：目標語系單選，開關預設值仍待確認。
- Q14 於 2026-10-04 確認 A：必要時另製作本機相容預覽，已記錄 ADR 0003。
- Q15 已確認 A：MP4 相容時優先採用，否則 MKV，影音不重新編碼，已補入 ADR 0003。
- Q16 已確認 A：本機影片庫跨次保存影片、字幕版本及對話，已補入 ADR 0001／0002。
- 設定查證完成：專案內僅有既有服務的 `.env.example`，未找到 `.env`／`.env.local` 或 Gemini 欄位。
- Q17 已確認 A：專案根目錄 `.env` 的 `GEMINI_API_KEY`；真實 `.env` 未建立。
- Q18 只寫入文件，尚未得到使用者回答；「繼續／交接」不等於 Q18 已確認。
- 原文開關預設關閉、模型候選、最近 8 組問答記憶皆為草案建議，非使用者已接受。
- 官方文件查證已記錄於 `docs/design/media-compatibility-notes.md`；預覽參數與支援組合待落實。
- 既有未追蹤 `package-lock.json` 不屬於本次訪談。
- 過往任務紀錄保留；先前本機轉錄選擇不推定為這次新增介面的運行型態。

## Dependencies and environment

- 現有專案：Python `>=3.13,<3.14`、uv、ffmpeg／ffprobe；待選定介面型態後再確認新增依賴。
- 設計訪談不需要實際 API key，也不需要影片下載或付費呼叫。

## Risk and rollback

- 本階段風險低：僅新增設計文件與進度紀錄，可逐項修訂。
- 實作風險須待運行範圍與外部呼叫契約確認後評估，屆時補上回復策略。

## Results

- Q1 已確認本機網頁並記錄 ADR；Q2 已確認 A + B，補入字幕切換／匯入詞彙及驗收行為。
- Q3 已確認 A，補入可切換字幕軌詞彙與成品驗收行為。
- Q4 已確認 A，記錄來源順序與三種驗收情境。
- Q5 已確認 B，補入影片依據／補充知識詞彙及混合回答驗收行為。
- Q6 已確認 A，記錄 Gemini 共用憑證與驗收行為。
- Q7 已確認 A，新增領域詞彙、ADR 0002 與來源選取驗收行為。
- Q8 已確認 A，補入版本／對話詞彙及舊引用、上下文隔離驗收。
- Q9 已確認 A，補入文字問答與畫面資訊缺失的驗收。
- Q10 已確認 A，記錄單支影片匯入與防止自動展開播放清單的驗收。
- Q11 已確認 A，補入可用畫質與單一成品驗收。
- Q12 已依使用者要求改成獨立原文開關，補入匯出字幕軌驗收。
- Q13 已確認 A，補入單選與原文開關獨立性驗收。
- Q14 已確認 A，新增預覽／成品詞彙、ADR 0003 與相容預覽驗收。
- Q15 已確認 A，補入格式選擇與來源串流保留驗收。
- Q16 已確認 A，補入本機影片庫詞彙與跨服務重啟驗收；Q17 Gemini 設定來源待回答。
- 已記錄容器相容性官方來源與產品取捨；已於 lessons 記錄避免綁定獨立設定的修正。
- 文件驗證：`git diff --check`；八份設計／進度／經驗文件的相對連結、行尾空白與換行檢查。
- Q17 已確認 A，寫入訪談、PRODUCT.md 與本清單。
- 2026-10-04 review 修訂（v1 規格）：
  1. 共同發佈條件：每次 running 新 attempt ID、刪除先標不可寫、發佈時原子核對；AC08 補兩個遲到結果情境。
  2. 匯出選擇快照：資產、翻譯來源、目標版本／語系、原文 flag／版本、容器；AC03 補排隊中改選。
  3. 影片主張至少一個有效引用，新增「缺乏依據」類別；AC07 補空引用、漏引用、人工語義樣本。
  4. 新增「以此版本翻譯」入口與翻譯來源摘要；Q4 自動順序僅在未指定匯入來源時適用；AC05 補。
  5. 播放切換只改播放選擇，零 provider 呼叫與零封裝；AC04 補。
  6. 對話記憶提案：來源完整＋最近 8 組成功問答＋目前問題，排除失敗／取消，介面顯示範圍；AC08／AC11 補。
- Gemini 設定契約補入 v1 第 8 節：顯式 `api_key`（避開 `GOOGLE_API_KEY` 優先）、固定 stable ID、關閉 SDK 自動重試。
- 完整設計尚未經 Q18 確認，未開始產品實作；本次未執行 Ruff／mypy／pytest（僅文件變更）。

---

# 2026-07-20 Replace Cloud Acceptance with Local MLX Transcription

## Goal and acceptance criteria

- [x] Transcribe the target MP4 locally without AssemblyAI or Anthropic credentials.
- [x] Use the installed MLX Whisper runtime and cached large-v3-turbo model; do not upload media.
- [x] Preserve canonical transcript JSON/Markdown, ordered in-range timestamps, raw text, and
  conservative Traditional Chinese normalization.
- [x] Represent the lack of local diarization honestly with an anonymous single-speaker fallback.
- [x] Prove the local path on a short excerpt before processing the complete 34:20 source.

## Plan

- [x] Stop the cloud live-acceptance plan after the user chose a local-only conversion.
- [x] Inspect local hardware, installed runtimes, cached models, and provider boundaries.
- [x] Create an OpenSpec change for a local MLX Whisper transcriber.
- [x] Add failing focused tests, then implement the smallest local backend and CLI/config wiring.
- [x] Verify a short local excerpt and compatible resume behavior.
- [x] Process the full source locally and inspect transcript artifacts.

## Working notes

- Local runtime: Apple Silicon with 48 GB RAM; `mlx_whisper` 0.4.3 is installed.
- Cached model: `mlx-community/whisper-large-v3-turbo` (about 1.5 GB), so no model download is
  required for the selected path.
- Ollama is installed, but only a cloud-backed model is present. A local report model is outside
  this minimal conversion slice; the first result will be canonical transcript JSON/Markdown.
- MLX Whisper provides segment/word timestamps but no speaker diarization. Local mode must use
  `講者 A` consistently and document that limitation instead of inferring identities.
- The Traditional Chinese initial-prompt experiment did not improve script consistency and was
  rejected. Output preserves raw ASR text and uses the existing conservative normalizer.
- Human auditory comparison is not possible in this interface. Five distributed transcript samples
  are recorded below, but OpenSpec task 4.3 remains unchecked until a person compares playback.

## Dependencies and environment

- Existing Python 3.13, `ffmpeg`/`ffprobe`, `mlx_whisper`, MLX, and cached model.
- No API keys and no cloud-provider calls.

## Risk and rollback

- Risk: medium; this changes production backend selection and the credential contract.
- Rollback: retain the existing AssemblyAI adapter and make local selection explicit/reversible;
  revert the new adapter/config wiring if local quality or compatibility is unacceptable.

## Results

- Added OpenSpec change `add-local-mlx-transcription`; 12/13 tasks are complete after recording this
  evidence. Only the five-sample human listening portion of 4.3 remains open.
- Implemented explicit `mlx` backend selection, local credential behavior, conditional dependency,
  backend/model resume identity, lazy adapter loading, canonical mapping, and `講者 A` fallback.
- Excerpt acceptance: 45.002-second AAC, 38 ordered/in-range segments, complete anchors, retained raw
  payload, and compatible resume with unchanged transcript/raw artifacts.
- Full output: 2,060.167 seconds, 1,807 ordered/in-range segments, complete anchors, no overlap over
  50 ms, no gap over 10 seconds, and a retained 1,385,731-byte raw local payload.
- Full compatible `--resume` skipped MLX and left raw/JSON/Markdown sizes and mtimes unchanged.
- Acceptance exposed and fixed: missing creation of a new ffmpeg output directory, relative manifest
  path double-prefixing on resume, and empty MLX silence segments aborting long transcription.
- Five distributed samples for human playback comparison:
  - 00:00:59 (`s0034`): `好還有就是完全替代人類通用劳動力的機器人`
  - 00:07:00 (`s0339`): `有什么区别吗`
  - 00:13:59 (`s0730`): `本来這個环境就很恶劣`
  - 00:20:59 (`s1097`): `稍前周期很長`
  - 00:29:59 (`s1577`): `如果有要想做這支股票的朋友`
- Final verification passed: `uv sync --dev`, Ruff, format check, strict mypy, and 360 offline tests.
- Canonical outputs are under `outputs/local-full/`; no report was generated and no cloud call ran.

---

# 2026-07-20 Resume Video CLI Live Acceptance

## Goal and acceptance criteria

- [ ] Complete OpenSpec tasks 12.1–12.5 only when both cloud credentials are intentionally available.
- [ ] Accept a 30–60 second excerpt before submitting the complete 34:20 source.
- [ ] Prove transcript timing/speakers/anchors, report grounding, and paid-call-safe resume behavior.
- [ ] Manually sample five full-source timestamps without rewriting unsupported ASR content.

## Plan

- [x] Locate the active OpenSpec change and read every apply context artifact.
- [x] Check live-test prerequisites without exposing credential values.
- [x] Validate the handoff and reconfirm the credential-free live gate/source probe.
- [ ] Create and accept the short audio-only excerpt.
- [ ] Verify the compatible `--resume` run does not rewrite raw provider responses.
- [ ] Run and inspect the full source only after excerpt acceptance.
- [ ] Mark OpenSpec tasks 12.1–12.5 complete only from observed live evidence.

## Working notes

- Active change: `build-video-transcript-report-cli` (`spec-driven`, 58/63 complete).
- Authoritative pending list: `openspec/changes/build-video-transcript-report-cli/tasks.md`, group 12.
- `ASSEMBLYAI_API_KEY`, `ANTHROPIC_API_KEY`, `VCC_ENABLE_LIVE`, and
  `VCC_LIVE_EXCERPT_PATH` remain absent in the resumed environment.
- No live or paid command was run. OpenSpec task 12.6 requires 12.1–12.5 to remain unchecked.
- Resume at the credential-presence check, then follow the README live-test command exactly.
- `docs/checkpoints/current-handoff.md` passed resume validation at 11,469 bytes; targeted
  OpenSpec state remains `58/63` with only tasks 12.1–12.5 pending.
- A passing `tests/test_live_acceptance.py` is necessary but insufficient for group 12: its
  unchanged-mtime assertion must be supplemented with provider call records for 12.3, plus the
  manual transcript/report, full-source, and five-playback checks required by 12.2–12.5.

## Dependencies and environment

- Python `>=3.13,<3.14`, `uv`, `ffmpeg`, and `ffprobe`.
- Intentional access to valid AssemblyAI and Anthropic API credentials.
- Target source: `視野環球財經robots_07-19-2026 22-11-19_1.MP4`.

## Risk and rollback

- Risk: medium because acceptance invokes paid cloud APIs and processes source-program audio.
- Guardrail: both `pytest -m live` and `VCC_ENABLE_LIVE=1` are required; excerpt acceptance gates the full upload.
- Rollback: stop on any excerpt failure; retain manifests/artifacts for diagnosis and use compatible `--resume`
  rather than repeating completed paid work.

## Results

- Live acceptance is blocked by missing intentional credentials and opt-in variables.
- Safe gate verification passed: with all live variables explicitly unset,
  `uv run pytest -m live tests/test_live_acceptance.py -q` exited 0 with one skipped test.
- Credential-free source probe passed: duration `2060.167s`, one HEVC video stream, one AAC stereo
  audio stream, and no subtitle streams.
- No application code or OpenSpec checkbox was changed during this continuation.

---

# 2026-07-20 Codex Context Handoff Skill

## Goal and acceptance criteria

- [x] Install a global `context-handoff` Skill under `~/.codex/skills`.
- [x] Support bounded `checkpoint` and drift-aware `resume` modes.
- [x] Archive and validate `docs/checkpoints/current-handoff.md` deterministically.
- [x] Emit a shell-safe command that starts a fresh Codex CLI session.
- [x] Verify structure, scripts, edge cases, and realistic forward use.

## Plan

- [x] Confirm environment and initialize the Skill with the official scaffold.
- [x] Implement the deterministic checkpoint helper and self-test.
- [x] Write concise Skill instructions and matching UI metadata.
- [x] Run compile, self-test, Skill validation, and isolated smoke checks.
- [x] Forward-test checkpoint and resume behavior with fresh subagents.
- [x] Review the final artifacts and record results.

## Working notes

- Target surface: Codex CLI.
- Trigger policy: user invokes the Skill when the existing `context-used` TUI item reaches 80%.
- Automation: reliable semi-automatic; no hooks, transcript JSONL parsing, compaction, or automatic session launch.
- Checkpoint path: `docs/checkpoints/current-handoff.md`; resume continues automatically unless material drift is found.
- Workspace is not a Git repository, so verification must not assume Git is available.
- First self-test exposed macOS `/var` versus `/private/var` path identity; the test now compares resolved paths. Checkpoint validation itself passed.
- Initial CLI smoke batch was rejected before execution because its cleanup used `rm -rf`; rerun uses only `rmdir` on known-empty temporary directories.
- Verification then found a Python 3.13 Ruff import rule and zsh's read-only `status` parameter; both are corrected before rerunning the full check set.
- Post-check inventory found generated `__pycache__`; self-test now disables import bytecode and compiles only into a temporary directory.
- Independent review found symlink escape, permission widening, unbounded archive/read, and invalid-bootstrap risks; implementation now fails closed, publishes `0600` archives atomically, and validates before bootstrap.
- A shell smoke falsely passed after its valid fixture had been removed; CLI success and failure paths now have independent return-code assertions inside the self-test.
- Re-review found a parent-directory TOCTOU window; all checkpoint and archive operations now use no-follow, descriptor-relative directory traversal and I/O, with a parent-replacement regression case.

## Dependencies and environment

- Codex personal Skill root: `/Users/yuhan/.codex/skills`.
- Python standard library only for runtime scripts.
- Existing `/Users/yuhan/.codex/config.toml` already displays `context-used`; no config change is required.

## Risk and rollback

- Risk: low; this adds a personal Skill and does not change hooks or Codex configuration.
- Rollback: remove `/Users/yuhan/.codex/skills/context-handoff`.
- Generated project checkpoints remain intact for audit unless explicitly removed.

## Results

- Installed `/Users/yuhan/.codex/skills/context-handoff` with `SKILL.md`, UI metadata, a deterministic checkpoint helper, and an artifact-free self-test.
- Helper enforces the 16 KiB contract, descriptor-relative no-follow path traversal, bounded reads, private atomic archives, structural validation, and validation-gated bootstrap output.
- Verification passed: Skill validator, bundled self-test, Ruff, strict mypy, checkpoint/resume forward-tests, and independent security review.
- Fresh checkpoint/resume forward-test completed the recorded task with 2 passing tests; latest security-hardened checkpoint forward-test archived two generations as `0600`, validated the new checkpoint, and left product source unchanged.
- Final review reported no remaining Critical or Important findings.
# 2026-07-20 Publish Initial GitHub Repository

## Goal and acceptance criteria

- [x] Create `huijoson/video_content_capture` from the current workspace as a private repository.
- [x] Include source, tests, project docs, OpenSpec artifacts, and dependency lock data.
- [x] Exclude source media, generated outputs, credentials, caches, and local agent/session state.
- [x] Push a verified initial `main` commit and confirm the remote branch matches locally.

## Plan

- [x] Verify GitHub CLI authentication and confirm the target repository name is available.
- [x] Audit the workspace for large files, credentials, generated artifacts, and local-only state.
- [x] Add publish safeguards and review the exact initial commit scope.
- [x] Run the repository verification suite and create the initial commit.
- [x] Create the private GitHub repository, push `main`, and verify remote state.

## Working notes

- GitHub CLI is authenticated as `huijoson`; `huijoson/video_content_capture` does not yet exist.
- The root source MP4 is approximately 1.5 GB and must remain local.
- Repository visibility defaults to private because the user did not request public exposure.
- Initial repositories have no pre-existing default branch to target with a pull request, so the
  reviewed initial commit will establish `main` directly.

## Dependencies and environment

- GitHub CLI `gh` with authenticated `repo` scope.
- Local `git`, Python `>=3.13,<3.14`, `uv`, and the existing project verification toolchain.

## Risk and rollback

- Risk: medium because publication copies local content to an external service.
- Guardrails: private visibility, explicit ignore rules, credential scan, staged-file and size review.
- Rollback: delete the new GitHub repository if publication scope is wrong; the local workspace and
  ignored media remain unchanged.

## Results

- Publish scope review passed: 73 intended files totaling approximately 1 MiB; the 1.5 GB source
  video, generated outputs, caches, local settings, and checkpoints are ignored.
- Credential-pattern review found only blank `.env.example` placeholders and a deliberately fake
  regression-test secret.
- Verification passed: `uv sync --dev`, Ruff lint, Ruff format check, strict mypy, and 360 offline
  tests.
- Created the private `huijoson/video_content_capture` repository and pushed the tracked `main`
  branch; the final local/remote commit identity was verified after publishing this result record.

---

## 2026-10-04 S1 — 本機服務、設定與影片庫骨架

### Acceptance criteria
- serve 獨立載入指定專案根目錄 .env；非金鑰選項 > 環境 > .env > 預設。
- loopback、Host、Origin 防護；Gemini key 不進 API、日誌、錯誤或持久資料。
- SQLite 版本／升級備份、跨重啟保存、running → interrupted；受控 ID 與原子發佈。
- 三欄空狀態頁面、窄視窗上下配置；AC09、AC10、AC12 的 S1 範圍。

### Checklist
- [x] 閱讀權威規格、既有 CLI 設定與 lessons。
- [x] 設定邊界：先失敗測試，再最小實作。
- [x] 儲存：先失敗測試，再 schema／備份／中斷／原子發佈骨架。
- [x] API 與 serve：先失敗測試，再 Host／Origin／redaction 防護。
- [x] 頁面：語意化、樸素、無假資料、無瀏覽器金鑰入口。
- [x] 完整 Ruff／format／mypy／pytest 與獨立差異審查。
- [x] 記錄結果、未驗證與自行選定細節。

### Risk & Rollback
- 中風險：新增本機 HTTP 邊界與 SQLite；僅 serve 使用新模組。
- 回復：停止 serve，保留完整影片庫與升級備份；以原 CLI 使用舊功能。
  不將新 schema 交給舊版、不刪影片庫。

### Dependencies & Environment
- Python 3.13、uv；FastAPI／Uvicorn／python-dotenv；標準庫 sqlite3。
- 全部測試離線與假金鑰；不讀真實 .env、不下載影片、不呼叫 Gemini。

### Working Notes
- 測試邊界已由本切片確認：serve CLI、設定 loader、HTTP API、安全與 SQLite／檔案。
- 既有工作樹含使用者的文件與 tasks 修改，保留；package-lock.json 不碰。
- S1 不實作佇列、下載、播放器功能、字幕或問答；模型僅回報設定名稱。


### Results
- 完成 serve 獨立設定、loopback／Host／Origin／redaction、read-only API 與純靜態頁面。
- SQLite schema v1、升級前備份、running → interrupted、受控 UUID 與拒覆寫原子發佈。
- 依賴以 uv add 安裝並鎖定 FastAPI 0.142.2、Uvicorn 0.53.0、python-dotenv 1.2.4；
  已查 PyPI 與 installed metadata，三者 Requires-Python >=3.10 並列 Python 3.13。
- red → green：config/API 初次為 ModuleNotFoundError；storage 初次亦缺模組。
  root permission 0755 → 0700、localhost → numeric loopback 均有先失敗再通過證據。
- 最終完整命令：uv run ruff check . && uv run ruff format --check . &&
  uv run mypy src && uv run pytest -q；exit 0，All checks passed、53 files already formatted、
  Success: no issues found in 35 source files；411 collected，410 passed／1 skipped。
- pytest 有 1 個 Starlette TestClient/httpx 棄用警告；未為消除警告增加額外依賴。
- git diff --check、vcc serve --help、git check-ignore --no-index .env 皆成功。
- Chrome 真實本機頁面（臨時空專案、無 key）：桌面三欄、390px 上下配置、無水平溢位、
  網址輸入與 Shift+Tab 可達 skip link；Impeccable detector []。測試服務已停止。
- 獨立唯讀審查通過；補足 app 重建持久化、中斷回復、含 fake key 例外與日誌測試。
- 未驗證：Safari、200% 真實縮放、screen reader、Gemini 真實帳戶權限；後續下載／
  翻譯／播放器／VLC 不屬 S1，未呼叫任何 provider 或下載真實影片。
- 自選細節：VCC_HOST／VCC_PORT 與非金鑰啟動覆寫；空環境 key 明確禁用；.env 不插值、
  拒 symlink；寫入缺 Origin 拒絕；GET 有跨站 Origin 亦拒絕；精確 Host port；access log 關閉；
  DB library.sqlite3／user_version=1；新 DB 不做空備份；備份 UUID；schema 不向下讀。
- 新 root／受控目錄 0700、DB／備份／暫存／成品 0600，既有目錄不自動 chmod；
  atomic helper 接收 BinaryIO、驗證後 hard-link 拒覆寫；完整 attempt gate 留 S2。
  這不是隔離同 OS 使用者惡意檔案競態的 sandbox（詳 storage module docstring）。
- 未改分支、commit 或 push；未讀真實 .env／key，package-lock.json 未碰。

## 2026-10-04 S2 — YouTube 查詢、下載、佇列與播放

### Acceptance criteria
- AC01 單片 URL、真實格式、固定選擇；AC06 來源 stream copy／播放判斷。
- AC08 本片 attempt／deleting 競態；AC09 重試、checksum 重用、重啟中斷。
- AC10 受控媒體 ID／同源寫入；AC12 語意化首次匯入與進度（實播待驗收）。

### Checklist
- [x] 讀規格、S1 與 lessons；確認測試邊界 Library／adapter／API。
- [x] 儲存 v2、備份與通用 transaction gate。
- [x] adapter URL／metadata／可取消下載：失敗測試後實作。
- [x] 單一背景媒體佇列、固定選擇、手動重試與空間預檢。
- [x] API、Range／位置保存與樸素頁面。
- [x] 完整 lint／format／strict mypy／pytest 與差異審查。
- [x] 記錄結果、真實驗收限制與自選細節。

### Risk & Rollback
- 中風險：本機背景媒體工作與 schema v2。停止服務並保留完整庫／升級備份；
  回復 S1 時使用升級前備份，不把 v2 交給 v1，不自動刪資料。

### Dependencies & Environment
- Python 3.13、uv、ffmpeg／ffprobe；新增 yt-dlp 經 uv add 鎖定。
- 全部 fake／fixture；不讀真實 key、不下載真實影片、不呼叫付費 API。

### Working Notes
- 保留工作樹原有 S1／文件修改；不碰 package-lock.json，不 commit／push／切分支。
- queued 留存但重啟不自動啟動；使用者明確 retry 才排程。

### Results
- 完成本切片 AC01；AC06／AC08／AC09／AC10／AC12 的來源下載、attempt fence、
  重試保存、媒體安全與首次匯入 UI 範圍。後續字幕、預覽、問答、成品不提前實作。
- schema v2 增加 videos metadata／位置／deleting、media_assets、完整 jobs 與 job_stages；
  v1 升級前備份。通用 gate 在 BEGIN IMMEDIATE 核對 attempt／running／有效影片，
  owner_validator 供 S3／S4 接上；階段 checkpoint 不提前把工作標完成。
- 單一背景媒體 worker；每次 running 新 attempt；取消 terminate 子程序群組；
  graceful shutdown 與重啟轉 interrupted、不自動排程。重試與新增工作均避免同選擇重複。
- 固定 format/audio IDs、估算至少四份串流拷貝（未知大小仍不確定；最低 64 MiB），
  保留經 probe 與交易 gate 發佈的階段，重試 checksum／fingerprint 相符才續接。
- yt-dlp Python API 在可終止的子程序中；argv ffmpeg stream copy 與 ffprobe 驗證 codec、
  高度、FPS、有限正時長。查詢只保存字幕語言／來源／副檔名，S3 可重新查詢取字幕。
- 受控資產媒體端點支援 Range；純 HTML/CSS/JS 畫質、音軌、已保存資產選擇、進度、
  取消重試、重新查詢與播放位置保存；字幕／翻譯／問答仍停用。
- 新依賴 uv add yt-dlp，鎖定 2026.8.19，範圍 >=2026.8.19,<2027；查官方 PyPI 與
  installed metadata：Requires-Python >=3.10、Python 3.13 classifier。
- Red-first 證據：Library 缺方法、API 404、來源語言不能證明原音、stage resume 缺介面、
  Range Content-Length 缺失、retry 重複選擇、metadata DB sentinel 均先失敗後通過。
- 最終完整命令 uv run ruff check . && uv run ruff format --check . && uv run mypy src &&
  uv run pytest -q：exit 0；All checks passed；59 files already formatted；
  Success: no issues found in 37 source files；465 collected，464 passed／1 skipped。
  仍有既有 Starlette TestClient/httpx 棄用警告。早期整合曾有缺 DownloadStages collection
  與長行格式檢查失敗，已完成介面並修正後重跑全套通過。
- 額外證據：1 秒本機合成 H.264/AAC 真實 ffmpeg stream copy／ffprobe；fake 子程序取消；
  transaction 遲到結果／deleting；離線 API Range／去重／背景回應／位置／重啟；
  JS 語法與 fake DOM 查詢→處理→播放／位置 smoke；git diff --check 通過。
- 未驗證：真實 YouTube 可取得性／網路錯誤、Chrome／Safari 實播、VLC（後續成品）、
  screen reader、200% 放大與真實鍵盤流程；所有測試無付費 API、真實下載或真實 key。
- 自選細節：H.264/AAC MP4，其餘保守 MKV；1.5 秒輪詢、不定下載進度；每跨 5 秒、
  pause／seek／頁面隱藏保存位置；stage 依 job 保存，後續清理切片處理孤兒／舊 checkpoint。
  query refresh=true 為明確重查，普通重複匯入不連網；不自動降級或重試。
- 修改僅 S2 模組／靜態頁、依賴、工作紀錄與離線測試；既有 CLI 程式本片未動；
  S1 測試 INSERT 改具名欄位以適應 v2。未碰 package-lock.json、分支、commit、push。

## 2026-10-04 S3 — 字幕、翻譯與軌道匯出

### Acceptance criteria
- AC02：人工→自動→確認無字幕才多語 ASR；清單失敗不得辨識。
- AC04／AC10：有界 SRT/VTT 匯入、不可變版本、獨立選擇、播放切換零工作。
- AC05：cue ID／時間保持、完整驗證、相符階段續接、缺 key 零呼叫、attempt fence。
- AC03／AC06：不可變匯出快照、stream copy、容器確認、實際軌數語言驗證及受控下載。

### Checklist
- [x] 讀取規格、ADR、詞彙、lessons 與 S1／S2 邊界。
- [x] 字幕儲存／匯入／取得 red → green。
- [x] Gemini adapter／分塊／重試 red → green。
- [x] 匯出快照／封裝／驗證 red → green。
- [x] API 與頁面整合、同源與獨立字幕選擇驗證。
- [x] 完整 Ruff／format／strict mypy／pytest 與審查。
- [x] 記錄結果、未驗證及自選細節。

### Risk & Rollback
- 中風險：schema v3／雲端工作／封裝。升級前 SQLite 備份；版本與成品追加。
- 回復：停止 serve，保存整個庫；舊程式只使用升級前備份，不刪除新資料。

### Dependencies & Environment
- Python 3.13、uv、ffmpeg／ffprobe；google-genai 用 uv add 並查證 metadata。
- 所有 provider 離線 fake；不讀真實 .env／key，不下載真實影片，不呼叫付費 API。

### Working Notes
- 保留既有未提交 S1／S2 變更；不碰 package-lock.json，不 commit／push／改分支。
- 翻譯與媒體共用單 worker 串行，保守限制 Gemini 並行為一；重啟不自動重送。
- 確認的測試邊界：API、Library、adapter、共同 publication gate、真實合成媒體。


### Results
- 完成 AC02／AC04／AC05 與 AC03 匯出矩陣／快照；AC06 容器及 AC10 惡意字幕／同源
  防護的 S3 範圍。S4 問答保留停用占位；未實作 S5 相容預覽／清理。
- schema v3 經 v0／v1／v2 升級與備份；不可變字幕／cue、獨立選擇、成品／工作快照。
- 平台人工→自動→確認無原文才 ASR；清單失敗／語系未知有字幕／指定軌消失不辨識。
  ASR 沿用 MLX loader 的原始多語片段，不套中文提示／正規化；使用可注入 adapter。
- Gemini SecretStr 顯式 key、attempts=1、count_tokens、structured schema、finish／ID／
  空文驗證；100 cues／塊與900,000 token 預檢、120秒 timeout；已完成塊透過 StageStore
  保存並依來源／hash／語系／模型／規則重用。未完整版本不能選用，舊 attempt 不寫入。
- 翻譯／字幕／匯出共用單 worker；重啟轉 interrupted，queued 不自動排程。
- 一份目標字幕與獨立原文開關；重複版本去重，同語系修正版軌名帶版本 ID。
  容器以0.1秒 stream copy＋ffprobe 預檢；完整版不轉碼影音，MP4 失敗而 MKV 可用時
  需重新確認新快照；兩者失敗保存來源與字幕。成品發佈經共同 gate、受控 ID 下載。
- 匯出空間估算來源×2＋字幕 UTF8 bytes×2＋16MiB；未知 ISO639 雙字母語系安全拒絕，
  常見來源語系內建映射、三字母代碼保留；zh-TW／zh-CN 由軌名區分。
- 檔名 UTF8 byte 上限，保留 Unicode、移除路徑字元；原子 hard-link 拒覆寫。
- 新增依賴 google-genai 1.75.0（>=1.75,<1.76），uv add／uv.lock；官方與 installed
  metadata/source 已確認 Requires-Python>=3.10、Python3.13 classifier、SDK必要介面。
- 最終指定命令 uv run ruff check . && uv run ruff format --check . && uv run mypy src &&
  uv run pytest -q：exit0，All checks passed，66 files already formatted，
  Success: no issues found in 40 source files；536 collected，535 passed／1 skipped。
  既有 Starlette TestClient/httpx 棄用警告仍存在。早期 red 測試／整合／format／mypy
  失敗已修正後重跑；測試 teardown 競態已以公開 job 完成等待修正並記錄 lessons。
- 額外驗證 node --check 靜態 JS、git diff --check；Impeccable detector []；
  真實小型 ffmpeg 四列矩陣／FFV1→MKV、離線 API 字幕／翻譯／成品下載通過。
- Chrome 合成影片實測：播放字幕／翻譯來源／匯出分開、缺 key 翻譯停用、MP4 摘要
  確認→成品驗證→可下載；鍵盤移到0.3秒換字幕仍0.3秒、沒有新增工作。
  390px content width390、所有input/select/textarea有label；還原viewport並停止暫存服務。
- 未驗證真實 YouTube、MLX模型實際辨識、Gemini帳戶／模型權限／語義品質／quota／
  token預檢精度、Safari、VLC切軌、screen reader、200%真實縮放。未讀真實key／.env，
  未下載真實影片／呼叫付費API；不碰package-lock.json、分支、commit或push。
- 修改清單：workspace storage/youtube/jobs/app、static index.html/workspace.js；新增
  subtitles.py/translation.py/exports.py；新增四份 S3測試；調整 storage/UI測試；
  pyproject.toml/uv.lock、README.md、tasks/todo.md/lessons.md。
