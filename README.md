# Video Content Capture (`vcc`)

`vcc` 是一個 Python 3.13 命令列工具：先檢查影音檔、只抽取音訊，再以
本機 MLX Whisper 或 AssemblyAI 進行中文語音辨識，最後可由 Claude 產生具逐字稿證據
連結的繁體中文白話報告。

目前第一版只提供本機 CLI。核心 pipeline 不依賴終端輸出，因此之後可重用於其他介面。

## 功能與安全邊界

- `probe`：不需要雲端金鑰，只用本機 `ffprobe` 讀取容器、長度與串流資訊。
- `transcribe`：可用本機 MLX（不上傳任何媒體）或 AssemblyAI（只上傳抽出的音訊）；
  產生逐字稿 JSON 與 Markdown。
- `report`：從 canonical transcript JSON 產生有證據 ID 的報告 JSON 與 Markdown。
- `run`：依序執行 probe、音訊抽取、轉錄、報告與成功後清理。
- 講者只標成 `講者 A`、`講者 B` 等匿名名稱，不推測真實身分。
- 所有報告時間戳與逐字稿連結均由程式依 canonical segment ID 產生，不採信模型寫出的時間。
- 預測、判斷與建議會標成 `講者觀點`，不代表獨立查證，也不是投資建議。

目標影片 `視野環球財經robots_07-19-2026 22-11-19_1.MP4` 約 **34:20**，
原始檔約 **1.545 GB**，內容為 HEVC 影像、一條 AAC stereo 音訊、無字幕。`vcc` 只抽取
AAC 音訊供轉錄，絕不把 1.545 GB MP4 當成轉錄上傳檔。

## 必要條件

- macOS、Linux 或其他可執行 Python/ffmpeg 的環境
- Python `>=3.13,<3.14`
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` 與 `ffprobe`
- 雲端指令所需的環境變數：
  - `ASSEMBLYAI_API_KEY`：使用 `assemblyai` backend 的 `transcribe` 與 `run`
  - `ANTHROPIC_API_KEY`：`report` 與 `run`
- 本機 MLX backend 需要 Apple Silicon Mac 與 `mlx-whisper`；`uv sync --dev` 會在相容平台
  安裝 runtime。模型預設為 `mlx-community/whisper-large-v3-turbo`。

確認本機工具：

```bash
python3 --version
uv --version
ffmpeg -version
ffprobe -version
```

`probe` 完全不需要雲端憑證。缺少對應憑證時，雲端指令會在上傳或付費請求之前失敗。

## 安裝與開發環境

```bash
uv sync --dev
uv run vcc --help
```

`pyproject.toml` 使用 uv 的 `[dependency-groups].dev`，所以全新環境中的
`uv sync --dev` 會安裝 pytest、pytest-mock、Ruff 與 mypy。

也可使用模組入口：

```bash
uv run python -m video_content_capture --help
```

它與 `uv run vcc` 等價。

## 憑證與設定

複製範例後填入真實值，或直接從 shell/秘密管理服務匯出：

```bash
cp .env.example .env
export ASSEMBLYAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
```

> 不要把真實金鑰放在 CLI 參數、README、測試 fixture 或版本控制中，也不要提交 `.env`。

常用可選環境變數：

```text
VCC_LANGUAGE=zh-TW
VCC_TRANSCRIPTION_BACKEND=assemblyai
VCC_MLX_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo
VCC_OUTPUT_DIR=./outputs
VCC_CACHE_DIR=./.vcc-cache
VCC_MIN_SPEAKERS=
VCC_MAX_SPEAKERS=
VCC_ASSEMBLYAI_MODEL=best
VCC_DEFAULT_ANTHROPIC_MODEL=claude-opus-4-8
VCC_MAX_RETRIES=5
VCC_RETRY_BASE_DELAY_SECONDS=1.0
```

設定優先順序為「CLI 明確值 > 環境變數 > 預設值」。金鑰使用 `SecretStr`，並由 CLI
的 redaction filter 從錯誤與日誌中移除。

## 命令

以下範例刻意保留中文與空白路徑；程式使用 subprocess argument array，不做 shell 字串插值。

### 1. 檢查影音（不需憑證）

```bash
uv run vcc probe "視野環球財經robots_07-19-2026 22-11-19_1.MP4"
```

可選參數：

```text
--output-dir PATH   指定輸出目錄
-v / --verbose      增加詳細程度
```

### 2. 只產生逐字稿

完全本機（Apple Silicon，不需要 API key）：

```bash
uv run vcc transcribe "視野環球財經robots_07-19-2026 22-11-19_1.MP4" \
  --transcription-backend mlx \
  --output-dir outputs/local-transcript
```

MLX Whisper 會在本機執行；若指定 Hugging Face repository ID 且模型尚未快取，首次模型
取得可能需要網路，但音訊與影片不會上傳。可將 `VCC_MLX_WHISPER_MODEL` 指向已下載的本機
模型目錄以保證離線啟動。MLX Whisper 不提供 speaker diarization，因此此 backend 會誠實地
將所有片段標成 `講者 A`，且不接受 `--min-speakers` / `--max-speakers`。

AssemblyAI（雲端）：

```bash
uv run vcc transcribe "視野環球財經robots_07-19-2026 22-11-19_1.MP4" \
  --transcription-backend assemblyai \
  --output-dir outputs \
  --language zh-TW \
  --min-speakers 2 \
  --max-speakers 5
```

`assemblyai` backend 需要 `ASSEMBLYAI_API_KEY`。未提供講者上下限時使用自動估計；若兩者都有值，必須
`--min-speakers <= --max-speakers` 且至少為 1。

### 3. 從逐字稿產生報告

```bash
uv run vcc report outputs/video.transcript.json --output-dir outputs
uv run vcc report outputs/video.transcript.md --output-dir outputs
```

需要 `ANTHROPIC_API_KEY`。若輸入是本工具產生的 `.transcript.md`，程式會解析同一目錄、
同一 stem 的相鄰 `.transcript.json`；不會重新解析 Markdown 文字。缺少相鄰 JSON 會以
configuration error 結束。

### 4. 完整流程

```bash
uv run vcc run "視野環球財經robots_07-19-2026 22-11-19_1.MP4" \
  --output-dir outputs
```

需要兩個金鑰。完整成功後，預設刪除抽出的音訊；加上 `--keep-audio` 可保留。

### 共用選項

| 選項 | 說明 |
|---|---|
| `--output-dir PATH` | 輸出目錄；省略時使用來源檔或逐字稿所在目錄。 |
| `--language CODE` | 語言，預設 `zh-TW`。 |
| `--transcription-backend NAME` | `assemblyai`（預設）或 Apple Silicon 本機 `mlx`。 |
| `--min-speakers N` | 可選的最少講者數。 |
| `--max-speakers N` | 可選的最多講者數。 |
| `--resume` / `--no-resume` | 嘗試重用相容且 checksum 正確的完成步驟；預設不 resume。 |
| `--force` | 開始新的 attempt，重新執行付費步驟並覆寫目前 deterministic artifacts。 |
| `--keep-audio` | `run` 成功後保留抽出的音訊；`transcribe` 本身不做成功清理。 |
| `-v`, `--verbose` | 可重複增加日誌詳細度。 |

`--resume` 與 `--force` 不應同時作為「既要重用又要重跑」的意圖；需要重跑時直接使用
`--force`。

## 輸出與 canonical schema

若來源 stem 為 `video`，輸出目錄大致如下：

```text
outputs/
├── video.transcript.json          # canonical 逐字稿
├── video.transcript.md            # 可閱讀逐字稿，含 segment anchors
├── video.report.json              # canonical grounded 報告
├── video.report.md                # 白話報告，含逐項證據連結與來源索引
├── video.metadata.json            # run metadata，不含憑證
├── video.manifest.json            # transcribe/run resume state
├── video.report.manifest.json     # standalone report resume state
├── video.m4a                      # 抽出的音訊；成功後可能被清除
└── video.raw/
    ├── transcribe.json             # 原始轉錄 provider/local payload
    └── report.json                 # 報告 provider metadata/payload
```

### Transcript JSON

Canonical transcript 包含：

- 原始媒體 metadata（來源路徑、容器、長度、音訊/影像/字幕串流）
- 語言
- 依時間排序的 segments
- 每段穩定 `segment_id`、`start`、`end`、匿名 `speaker_label`
- `raw_text` 與保守正規化後的 `normalized_text`
- 可用時的 words、信心與字詞時間

Transcript Markdown 每段使用穩定 anchor，標題格式為：

```text
HH:MM:SS–HH:MM:SS｜講者 X
```

並顯示 ASR 可能有誤的警告。

### Report JSON 與 Markdown

Canonical report 保留六個 section：

1. `三分鐘掌握影片`
2. `核心重點`
3. `重要數字與說法`
4. `名詞白話解釋`
5. `結論與可能影響`
6. `來源索引`

每個來源相關 item 都保留 `source_segment_ids`；需要時保留
`is_speaker_opinion`。Markdown 依這些 ID 產生逐項時間戳與 transcript anchor 連結，並將
預測或建議標成 `講者觀點`。模型不提供權威時間戳，也不直接生成 Markdown。

## Resume、冪等性與中斷

- Source SHA-256 以串流方式計算，不會把 1.545 GB 檔案一次讀入記憶體。
- Cache/manifest identity 結合來源內容與有效處理設定；金鑰不在 identity 中。
- 完成步驟只在 JSON、Markdown、raw payload 都成功寫入並記錄 checksum 後才更新 manifest。
- `--resume` 只跳過 identity 相容且所有必要 artifacts 存在、checksum 正確的步驟。
- 來源或設定不同會回報 resume mismatch（exit 8），並提示 `--force`。
- 完成狀態中的檔案遺失或損壞會回報 filesystem error（exit 9）；不會靜默重做付費請求。
- `--force` 建立新的 attempt，重新執行付費步驟。
- 在等待 provider 時按 `Ctrl-C` 會以 130 結束；最後一個已 atomic 完成的 manifest 仍可讀，
  下一次可用相容的 `--resume` 繼續。
- 清理只在完整成功後進行。失敗或中斷會保留診斷與 resume 所需檔案。

## Retry 與錯誤分類

僅下列暫時性錯誤會做有限次數的 exponential backoff：

- timeout / connection timeout
- HTTP 429
- HTTP 5xx / provider overloaded

驗證失敗、401/403、無效輸入、unsupported media、malformed provider payload、grounding
錯誤不做暫時性重試。SDK 內建 retry 在 adapter 中關閉，避免與應用層重試相乘。

## Exit codes

| Code | 類別 | 說明 |
|---:|---|---|
| 0 | Success | 成功。 |
| 1 | Unexpected | 未分類的非預期錯誤。 |
| 2 | Media | 缺檔、沒有音訊、ffmpeg/ffprobe 或媒體錯誤。 |
| 3 | Configuration | 設定、憑證、speaker bounds 或相鄰 JSON 錯誤。 |
| 4 | Provider authentication | Provider 認證或權限錯誤。 |
| 5 | Rate limit / transient exhausted | 429、timeout 或 5xx 重試用盡。 |
| 6 | Provider payload | 永久性 4xx、無效或 malformed provider 回應。 |
| 7 | Grounding | 未知 evidence ID、空 evidence、超出時間範圍或 section 形狀錯誤。 |
| 8 | Resume mismatch | 來源/逐字稿或設定與 manifest 不相容。 |
| 9 | Filesystem | 必要 artifact 遺失、checksum 不符或檔案系統失敗。 |
| 130 | Interrupted | 使用者按下 Ctrl-C。 |

## 隱私、資料保留與雲端處理

- 轉錄會把抽出的音訊送往 AssemblyAI；報告會把結構化逐字稿 segments 送往 Anthropic。
- 選擇 `--transcription-backend mlx` 時，轉錄在 Apple Silicon 本機執行，媒體不會送往
  AssemblyAI；單獨執行 `transcribe` 也不會呼叫 Anthropic。
- 原始 MP4 不會上傳，但音訊本身仍可能包含敏感資訊。使用前請確認你有權處理與傳送內容。
- 本機 raw payload、JSON、Markdown、manifest 與 metadata 可能包含逐字內容，請依組織的資料保留、
  加密與刪除政策管理。
- 預設只在完整成功後刪除抽出的 audio；raw provider data 與 canonical artifacts 會保留供稽核與 resume。
- `.env` 與 output/cache 目錄已在 `.gitignore`，但仍應確認備份與同步工具不會外洩資料。
- 請另外查閱 AssemblyAI 與 Anthropic 的最新隱私、資料保留與區域政策。

## 成本估算

不要把 README 中的固定單價當成最新價格；實際價格可能依日期、方案與區域改變。執行前請查看：

- AssemblyAI pricing：音訊分鐘數 × 所選 speech model/方案單價，另考慮 speaker diarization、
  language 與其他功能是否影響價格。
- MLX 本機轉錄沒有 provider 用量費，但會消耗本機時間、記憶體與磁碟；首次取得未快取模型
  可能使用網路流量。
- Anthropic pricing：模型（預設 `claude-opus-4-8`）的 uncached input tokens、cache-write tokens、
  cache-read tokens 與 output tokens各自乘上最新單價。長逐字稿會使用 chronological map/reduce，
  因此可能有多次 request。
- Retry 可能增加請求次數；resume 可避免重複已完成的付費步驟。

建議先建立 30–60 秒的 audio-only excerpt，接受逐字稿、講者與 evidence 品質後，再送出完整 34:20
來源。這也是 group-12 live acceptance 的必要順序。

## 測試

預設測試完全離線，provider 與 subprocess 呼叫使用 mocks/owned fixtures：

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Repository-owned fixtures 位於 `tests/fixtures/`，包括一秒 public-domain synthetic WAV 與手工撰寫的
provider JSON。它們不含原始節目片段、個資或真實金鑰。

Live excerpt 測試必須同時明確選取 marker 與 opt-in 環境變數；只設其中一項不會呼叫 provider：

```bash
export VCC_ENABLE_LIVE=1
export VCC_LIVE_EXCERPT_PATH="/absolute/path/to/30-60-second-excerpt.m4a"
export ASSEMBLYAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
uv run pytest -m live tests/test_live_acceptance.py -q
```

完整來源只應在短 excerpt 驗收通過後手動執行。不要把 live marker 加進一般 CI。

## Troubleshooting

### `ffmpeg` 或 `ffprobe` 找不到

安裝 ffmpeg，確認 `ffmpeg -version` 與 `ffprobe -version` 都成功，並檢查 `PATH`。

### Media file not found / no audio stream

確認路徑與權限，先執行 `vcc probe`。只有影像、沒有音訊的檔案不能轉錄。

### 缺少憑證

依命令設定 `ASSEMBLYAI_API_KEY`、`ANTHROPIC_API_KEY`。不要把金鑰當成 CLI flag。

### Speaker bounds invalid

`--min-speakers` 與 `--max-speakers` 必須至少為 1，且 min 不可大於 max。若不確定，兩者都省略。

### `.transcript.md` 找不到相鄰 JSON

`report` 必須讀取同 stem 的 `.transcript.json`。移動 Markdown 時也要一起移動 canonical JSON，或直接傳入 JSON。

### Resume mismatch（exit 8）

來源內容、逐字稿或有效設定已改變。確認參數後，若確實要重新執行，使用 `--force`。

### Filesystem/checksum error（exit 9）

完成狀態中的 JSON、Markdown 或 raw payload 遺失或被修改。從備份復原，或確認願意重新付費後用 `--force`。

### 429、timeout 或 5xx（exit 5）

等待 provider 恢復、檢查 quota，之後使用 `--resume`。不要無限重試。

### Grounding error（exit 7）

模型輸出的 evidence ID、section 或 speaker-opinion 標記無效。保留 raw response 供診斷；不要手工捏造證據。

### 磁碟空間不足

檢查 output/cache、`.raw/` 與保留的 `.m4a`。只有在不需要稽核/resume 且已有備份時才手動刪除。
