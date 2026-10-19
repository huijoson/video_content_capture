# One-click flow

One-click flow downloads the selected resolution, obtains subtitles, translates when the target language differs, and exports a video. The user starts it with `開始處理`.

## Sub-features

- `flow-start` starts processing from the playback pane.
- `flow-blocked` names the missing step and leaves the start button disabled.
- `flow-cancel` cancels a running flow.
- `flow-retry` retries a failed, cancelled, or interrupted flow.

## How to get to it (user POV)

- Load or open a video, then choose `開始處理` in the `一鍵流程` form.
- `取消流程` and `重試流程` appear in that form only while they apply.
- There is no other start control. The export panel is a separate path and is not this feature.

## Driving it with control-vcc

Preconditions:

- Doctor passes.
- A video is open and `#video-details` is visible.
- You accept that a start contacts YouTube and may contact Gemini. If you cannot accept that, stop. Do not stub the download.

- **Read the gate.** Run `control-vcc browser visible --id flow-blocked`. When it prints `visible`, run `control-vcc browser text --id flow-blocked`. A missing Gemini key reads `需要翻譯但尚未設定 Gemini 金鑰：請在專案 .env 設定 GEMINI_API_KEY 並重啟服務。` An unresolved platform subtitle reads `原語系未知但平台已有字幕：請在「字幕版本」選擇平台字幕並按「取得所選平台字幕」，再按「以此版本翻譯」指名來源，之後即可開始一鍵流程。` `開始處理` stays disabled. That visible notice is the proof of the gate. Do not click start.
- **Start when it is enabled.** Run `control-vcc browser click --role button --name "開始處理"`. Then run `control-vcc browser wait --id flow-status --any "流程待處理" "流程排隊中" "流程處理中" "流程已完成" "流程失敗" "流程已取消" "流程已中斷" --timeout 180`.
- **Cancel.** When `取消流程` is visible, run `control-vcc browser click --role button --name "取消流程"`. Wait until `#flow-status` contains `流程已取消`.
- **Retry.** When `重試流程` is visible, run `control-vcc browser click --role button --name "重試流程"`. The status leaves the failed, cancelled, or interrupted sentence.
- **Finished export.** A finished flow shows a link named `下載影片` and `#flow-status` contains `流程已完成`. Follow the link only through the page. Confirm the downloaded file is non-empty.
- **Proof.** Capture the status you actually reached with `control-vcc browser snapshot --aria --path one-click-flow/status.aria.txt` and `control-vcc browser screenshot --path one-click-flow/status.png`.

## Gotchas

- The default subtitle form is `燒錄字幕（預設）`. Burned output needs a translation whenever the target language is not already the original, and that translation needs Gemini. A missing key blocks start. Switching the form is a user action in the `調整` disclosure, not a way to skip the gate quietly.
- Restarting the server does not resume a running flow. An interrupted flow stays interrupted until the user chooses `重試流程`.
- `開始處理` is disabled while a flow for this video is active. The summary then says `已有一鍵流程正在進行；請先取消或等它完成。`
- Progress text is not a finished video. Only `下載影片` plus a non-empty file proves the export.
