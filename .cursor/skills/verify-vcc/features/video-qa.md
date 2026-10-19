# Video Q&A

Video Q&A asks a question against one complete subtitle version and keeps the answer in a saved conversation.

## Sub-features

- `qa-needs-source` tells the user to obtain or import subtitles before asking.
- `qa-needs-key` leaves the question box disabled when Gemini is not configured.
- `qa-ask` sends a question and shows a verified reply in the conversation.
- `qa-new-source` starts a new conversation when the user changes the subtitle basis.

## How to get to it (user POV)

- Use the `影片問答` column. The question field is labeled `問題` and the submit button is `送出問題`.
- Choose the basis with `問答依據（完整字幕版本）` and `設為問答依據`.
- At a window width of 60rem or below, choose the `問答` tab first. The harness window is 1280 by 900, so the column is already on the page.

## Driving it with control-vcc

Preconditions:

- Doctor passes and prints `gemini_configured=true`. A false value means the key precondition is unmet for `qa-ask`. Record it and stop that sub-feature.
- A video is open and one complete subtitle version exists. If `#qa-status` is `尚未選取影片` or `請取得／匯入字幕並設為問答依據。`, stop. Load, one-click flow, or a user import has to happen first. Do not invent a subtitle row.

- **No source.** With a video open and no complete subtitle, `#qa-no-source` is visible and tells the user to obtain or import subtitles. `送出問題` is disabled.
- **Set the basis.** Choose a complete version in `問答依據（完整字幕版本）`, then run `control-vcc browser click --role button --name "設為問答依據"`. `#qa-status` becomes `已建立新對話`, then `可提出問題；回覆將先整份驗證。`
- **Ask.** Run `control-vcc browser fill --role textbox --name "問題" --value "這支影片的第一句是什麼？"`. The `送出問題` button becomes enabled. Run `control-vcc browser click --role button --name "送出問題"`.
- **Reply.** Run `control-vcc browser text --id qa-status`. A reply still in flight reads `問題已送出，正在等待完整且驗證通過的回覆。` Then run `control-vcc browser wait --id qa-status --text "可提出問題；回覆將先整份驗證。" --timeout 180`. The `問答訊息` list contains the question and a reply.
- **Stored conversation.** Run `control-vcc read /api/videos/<video id>/conversations`. The current conversation id is listed. Then `control-vcc read /api/conversations/<conversation id>` contains the question text.
- **Proof.** Run `control-vcc browser snapshot --aria --path video-qa/reply.aria.txt` and `control-vcc browser screenshot --path video-qa/reply.png`.

## Gotchas

- On an empty library the question box is disabled because no video is selected. That does not prove the missing-key gate. Prove the key gate only with a complete subtitle selected and `gemini_configured=false` from doctor. The question box stays disabled and `#gemini-help` stays visible.
- A pending reply is not a verified reply. Wait until the status returns to `可提出問題；回覆將先整份驗證。`
- Changing the basis with `設為問答依據` starts a new conversation. The previous conversation remains in `已保存的對話`.
- Enter submits when the question box is focused. Shift+Enter inserts a newline. A harness `click` on `送出問題` is the unambiguous path.
