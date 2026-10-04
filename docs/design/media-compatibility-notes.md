# 影片容器、字幕與瀏覽器預覽查證

查證日期：2026-10-03。決策更新：2026-10-04。用途：支援設計訪談的格式與預覽決策。

## 官方資料支持的事實

- FFmpeg stream copy 不解碼或重新編碼，可保留選定影音串流而不產生重新壓縮的品質
  損失；容器不支援來源串流時仍可能失敗。
  [FFmpeg Streamcopy](https://ffmpeg.org/ffmpeg.html#Streamcopy)
- yt-dlp 官方字幕封裝實作對 MP4／MOV／M4A 使用 `mov_text`；匯入 SRT／VTT 後可能
  需要字幕格式轉換，不能承諾保留所有樣式與附加資訊。
  [yt-dlp FFmpeg postprocessor](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/postprocessor/ffmpeg.py)
- Matroska 規格列出 AV1、VP9、AAC、Opus 與文字字幕的 codec mappings。播放器能否
  解碼特定組合，仍須測試。
  [Matroska codec mappings](https://www.matroska.org/technical/codec_specs.html)
- 容器副檔名不足以判斷瀏覽器相容性，還需考慮影音編碼與平台；MP4 也可承載 AV1
  或 VP9，不能把高畫質一概視為只支援 MKV。
  [MDN video codecs](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Video_codecs)
- yt-dlp 的 `--merge-output-format` 在不需要合併時會被忽略；`--remux-video` 遇到
  不相容的編碼會失敗。`-t mp4` 的格式排序偏好也不等於已保證指定畫質。
  [yt-dlp video format options](https://github.com/yt-dlp/yt-dlp/blob/master/README.md#video-format-options)
- HTML `<track>` 使用 WebVTT 並可提供語言及標籤；網頁字幕切換可以使用獨立 VTT，
  不必依賴瀏覽器解析下載成品的內嵌字幕。
  [MDN track](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/track)
- `canPlayType()` 提供播放能力的可能性結果，不能替代實際播放驗收。
  [MDN canPlayType](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/canPlayType)

## 設計取捨與確認狀態

- 匯出與預覽需要分別驗證。Q15 已確認成品保留來源影音串流；網頁以獨立 VTT 切換
  字幕是待落實的技術方案。
- Q15 已確認格式相容時優先 MP4，否則使用 MKV，匯出前顯示實際格式；不為固定
  副檔名重新壓縮影音。各支援組合仍需實際驗收。
- Q14 已確認必要時另產生相容預覽，接受額外本機處理與儲存以維持網頁內觀看。
  參見 [ADR 0003](../adr/0003-separate-preview-and-export.md)。
- 不承諾固定 MP4、所有畫質、所有瀏覽器直接播放與完全不轉碼可以同時成立。
- 瀏覽器及外部播放器驗收清單、預覽畫質及產生時機，仍待設計確認。

本次僅查閱官方文件與公開原始碼，未下載影片、測試實際播放器或呼叫付費服務。
