# Load a video

Load a video looks up one public video URL (YouTube, X, or another yt-dlp site) and adds it to the library. It does not download the media.

## Sub-features

- `load-submit` queries a single public video URL from the header form.
- `load-reject` shows one failure sentence for a URL the workspace will not query.
- `load-cancel` stops a query that is still running.

## How to get to it (user POV)

- Type in the field labeled `影片網址（YouTube、X 等）` and choose `載入影片`.
- That form is the only entry. Opening a video already in the library is search, not load.

## Driving it with control-vcc

Preconditions:

- Doctor passes.
- The library does not already contain the video id you are about to load.
- The network path to the video site is available. If it is not, stop and record that. Do not substitute a fixture row.

- **Enter a URL.** Run `control-vcc browser fill --role textbox --name "影片網址（YouTube、X 等）" --value "https://www.youtube.com/watch?v=<11 character id>"`. The field shows that URL.
- **Load.** Run `control-vcc browser click --role button --name "載入影片"`. While the query runs, `#import-status` is `正在查詢影片資料；尚未開始下載…`.
- **Loaded.** Run `control-vcc browser wait --id import-status --text "影片已載入，請確認解析度後開始處理。" --timeout 60`. `#video-title` shows the source title. `#library-status` is `已保存 1 支影片` when this was the first save.
- **Stored row.** Run `control-vcc read /api/videos`. The JSON contains the same title.
- **Reject a non-video URL.** Fill `https://example.com/not-a-video` and click `載入影片`. `#import-status` becomes `來源不支援／無法取得。請確認網址是公開的單支隨選影片，且無須登入。`. `control-vcc read /api/videos` does not gain a row.
- **Proof.** Capture the loaded page with `control-vcc browser snapshot --aria --path load-video/loaded.aria.txt` and `control-vcc browser screenshot --path load-video/loaded.png`.

## Gotchas

- Every query failure uses the same status sentence. The sentence does not say whether the URL was malformed, private, or unreachable.
- A loaded video is metadata only. `#player` stays hidden until a download exists. Do not treat load as a finished download.
- `取消查詢` is hidden until a query is in flight. After a cancel, the status is `已取消查詢；不會建立下載工作。若服務已完成查詢，影片會出現在影片庫。` Check `read /api/videos` before deciding that nothing was saved.
- Re-entering the same video URL loads the existing library row instead of creating a second one.
