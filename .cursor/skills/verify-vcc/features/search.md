# Search titles

Search filters the saved library by title and leaves the saved videos in place.

## Sub-features

- `search-match` shows only titles that contain the query.
- `search-empty` shows a zero count when saved videos exist and none match.
- `search-clear` restores the full saved count.
- `search-open` opens a saved video from a result button.

## How to get to it (user POV)

- Use the search box labeled `搜尋標題` in the `影片庫` pane. There is no separate search button. The list updates as the user types.

## Driving it with control-vcc

Preconditions:

- Doctor passes.
- The library contains two saved videos whose titles are known, one of which contains `季報` and one of which does not.
- If `control-vcc browser text --id library-status` is `尚無影片。`, stop. The precondition is unmet. Loading a video is a separate feature.

- **Read the list.** Run `control-vcc browser snapshot --aria --path search/before.aria.txt`. Each saved video is a `button` whose accessible name starts with the title, then the duration and `位置`.
- **Title match.** Run `control-vcc browser fill --role searchbox --name "搜尋標題" --value "季報"`. `#library-status` reads `符合「季報」的影片 1 支（共 2 支）`. The matching button stays. The other title's button is gone.
- **No match.** Run `control-vcc browser fill --role searchbox --name "搜尋標題" --value "不存在的標題"`. The status reads `符合「不存在的標題」的影片 0 支（共 2 支）`. `#library-empty` is hidden.
- **Clear.** Run `control-vcc browser fill --role searchbox --name "搜尋標題" --value ""`. The status reads `已保存 2 支影片`.
- **Open a result.** Run `control-vcc browser click --role button --name "<name from the snapshot>"` using the full accessible name. `#import-status` becomes `已開啟影片；開啟不會自動建立下載工作。` and `#video-title` is the saved title.
- **Stored rows.** Run `control-vcc read /api/videos`. Both titles are still present. Search does not delete videos.
- **Proof.** Run `control-vcc browser snapshot --aria --path search/match.aria.txt` and `control-vcc browser screenshot --path search/match.png` while the matching query is in the box.

## Gotchas

- An empty library never produces `符合「…」的影片`. That sentence is the proof that saved videos were filtered.
- Matching is on the title only. A query that appears only in a subtitle or a question does not match.
- The result button's accessible name includes duration and position. Pass the full name from the snapshot to `click`.
- Opening a result selects that video in the playback pane. Clear the query before proving another filter.
