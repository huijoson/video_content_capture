# Library

The library shows every saved video, an empty state when there are none, and the service line for this local workspace.

## Sub-features

- `library-open` shows the workspace title, the service line, and the Gemini notice when no key is configured.
- `library-empty` shows the empty copy and the status `尚無影片。` when no video is saved.
- `library-search-empty` keeps that empty state when the search box has a query and the library has no videos.

## How to get to it (user POV)

- Open the workspace URL printed by launch. The library pane is on the page.
- The heading is `影片庫`. The search box is labeled `搜尋標題`.

## Driving it with control-vcc

Preconditions:

- `control-vcc doctor` prints `gemini_configured=false` and the checkout version.
- `control-vcc read /api/videos` is `[]`.

- **Open the workspace.** Run `control-vcc browser open`. The command prints a service line containing `Gemini 未設定` and a library line `尚無影片。`.
- **Service line.** Run `control-vcc browser text --id service-status`. The text contains `服務 0.1.0 · Gemini 未設定` until the package version changes, plus the translation and Q&A model names.
- **Gemini notice.** Run `control-vcc browser visible --id gemini-help`. The command prints `visible`.
- **Empty library.** Run `control-vcc browser text --id library-status` and `control-vcc browser visible --id library-empty`. The status is `尚無影片。` and the empty region is visible, including `影片庫目前是空的。`.
- **Query with nothing saved.** Type a title that is not saved. Run `control-vcc browser fill --role searchbox --name "搜尋標題" --value "不存在的標題"`. The status stays `尚無影片。` and `#library-empty` stays visible.
- **Clear the query.** Run `control-vcc browser fill --role searchbox --name "搜尋標題" --value ""`. The status is still `尚無影片。`.
- **Stored rows.** Run `control-vcc read /api/videos`. The body is `[]`.
- **Proof.** Run `control-vcc browser snapshot --aria --path library/empty.aria.txt` and `control-vcc browser screenshot --path library/empty.png`. Both artifacts show `YouTube 影片工作區`, `Gemini 未設定`, and `尚無影片。`.

## Gotchas

- `尚無影片。` after a query is the empty library, not a no-match search. No-match proof lives in the search feature and needs saved videos.
- Read the version from `#service-status` and from doctor. The two strings have to match.
- `#library-empty` stays visible for any query while the saved list is empty. Hiding it would mean a video was saved.
- Do not seed rows by writing SQLite. The empty library is the state launch creates.
