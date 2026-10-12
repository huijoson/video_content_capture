---
name: 影片內容擷取 · 字幕校對格
description: 本機 YouTube 影片工作區的視覺系統：檯面紙底、紙白工作表、1px 校對線、墨黑實心主要動作；in/out 紅與版本綠是僅有的兩個彩色，全系統零陰影、零藍色。
colors:
  desk: "#eceae4"
  sheet: "#ffffff"
  recess: "#f2f0eb"
  ink: "#1a1a1c"
  ink-muted: "#55555a"
  rule: "#c9c6bf"
  rule-strong: "#8a8781"
  mark: "#a8341f"
  ok: "#2f6f4f"
  primary: "#1a1a1c"
  primary-text: "#ffffff"
  primary-hover: "#000000"
  danger: "#a8341f"
  danger-text: "#ffffff"
  focus: "#1a1a1c"
  selection: "#e6d5cd"
  backdrop: "rgb(26 26 28 / 55%)"
  step-1: "#1a1a1c"
  step-2: "#3a3a3e"
  step-3: "#5a5a5f"
  step-4: "#7b7b81"
  step-5: "#9c9ca2"
  step-6: "#bdbdc2"
  step-7: "#dedee1"
  step-8: "#ffffff"
typography:
  h1:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.55
  h2:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "1.125rem"
    fontWeight: 700
    lineHeight: 1.55
  h3:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.55
  h4:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "0.9375rem"
    fontWeight: 700
    lineHeight: 1.55
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "0.9375rem"
    fontWeight: 600
    lineHeight: 1.55
  small:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.55
  micro:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.55
    letterSpacing: "0.08em"
  mono:
    fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace'
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.25
rounded:
  default: "0.5rem"
  small: "0.375rem"
  round: "50%"
spacing:
  1: "0.25rem"
  2: "0.5rem"
  3: "0.75rem"
  4: "1rem"
  5: "1.25rem"
  6: "1.5rem"
components:
  button-default:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px"
  button-default-hover:
    backgroundColor: "{colors.recess}"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-text}"
    rounded: "{rounded.small}"
    padding: "8px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-danger:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.danger}"
    rounded: "{rounded.small}"
    padding: "8px"
  button-danger-solid:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.danger-text}"
    rounded: "{rounded.small}"
    padding: "8px"
  button-selected:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px"
  input:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px"
  card:
    textColor: "{colors.ink}"
    padding: "12px 0 0"
  registry-field:
    textColor: "{colors.ink}"
    padding: "8px 12px"
  cue-line:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px 12px"
  video-list-item:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px"
  video-list-item-current:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px"
  status-mark:
    textColor: "{colors.ink}"
    size: "0.625rem"
    width: "1.1em"
    height: "0.625rem"
  worksheet:
    backgroundColor: "{colors.recess}"
    rounded: "{rounded.default}"
    padding: "16px"
    width: "100%"
  flow-stage-row:
    textColor: "{colors.ink}"
    padding: "4px 8px"
    width: "100%"
  flow-stage-row-running:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    padding: "4px 8px"
  notice:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px 12px"
  callout:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "8px 12px"
  dialog:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.default}"
    padding: "16px 24px"
    width: "min(32rem, calc(100vw - 2rem))"
---

# Design System: 影片內容擷取 · 字幕校對格

## Overview

**Creative North Star: "字幕校對格 Cue-Sheet Bench"**

這是一張擺在重製現場的校對表，不是一個網站。桌面是紙底的，工作表是紙白的，工作靠欄位
對齊與 1px 校對線完成——不靠浮起的卡片、不靠陰影、不靠色塊。三個工位（影片庫、播放與
字幕、影片問答）攤在同一張紙上，彼此之間只有一條線；每一列資料都落在自己該在的欄位上，
所以「哪一步在跑、哪個版本被選中、成品在哪」永遠一眼可見。

彩色預算近乎零：整頁只有兩個彩色——in/out 紅與版本綠——而且兩者都只在有語意時出現。
主要動作是墨黑實心按鈕，不是彩色按鈕；選取狀態是退紙底加墨色框，不是彩色底。深淺兩套
token 都跟隨系統偏好，不提供手動切換。

這個世界只出借字體、調色盤、密度與一個簽名動作（版本登錄列的 2px 紅記號），從不出借
版面、導覽或控制項：三欄佈局、元素 id、流程與全部繁中文案都沿用同一個產品契約。介面
本身不說話，它讓時間碼、版本與狀態文字說話。

**Key Characteristics:**
- 檯面紙底（#eceae4）上一張紙白工作表（#ffffff）；退紙（#f2f0eb）只給 hover、停用與
  需要低調的區塊。全系統零陰影、零玻璃、零漸層，深度一律是一條線。
- 只有兩個彩色：in/out 紅（#a8341f）與版本綠（#2f6f4f）。沒有藍色——舊系統的 #2856c7
  已完全退役，不留任何殘餘。
- 墨黑實心是主要動作的顏色（`--color-primary`＝`--color-ink`），不是彩色；破壞性動作
  才以紅字紅框出現。
- 等寬字體是量測工具，不是風格：時間碼、時長、版本代號、解析度、階段數字才用等寬，
  且一律 tabular numerals。
- 全系統兩個矩形圓角（8px 容器、6px 控制項）加上 `--radius-round` 一個真圓，真圓只用於
  畫出來的圓形（狀態記號的圓環與圓點、校準環）。
- 狀態一律「畫出來的記號 ＋ 繁體中文文字」成對出現，記號是 `aria-hidden` 的裝飾，
  顏色永遠不是唯一訊號。

## Colors

一個紙色系統加上兩個語意彩色；彩色只標記「這裡有狀態」，不裝飾。

### Primary
- **In/out 紅（In/Out Red）**（#a8341f，深色模式 #ff9b85）：世界裡唯一的紅鉛筆。它標出
  破壞（`button.danger` 的紅字紅框、`#delete-confirm` 的紅底白字）、失敗
  （`.error-text`、`.warn`、四階段流程中 failed／cancelled／interrupted 的狀態文字）、
  播放中字幕線的 `IN`／`OUT` 邊界（`.cue-edge`），以及版本登錄列「目前做到哪」的那一道
  2px 記號（`.registry-mark`）。文字輸入的 caret 也是這個紅（`caret-color`）。
  它從不出現在完成、選取、裝飾底色或任何安全動作上。

### Secondary
- **版本綠（Version Green）**（#2f6f4f，深色模式 #7ac596）：只標完成。用於 `.done` 與
  四階段流程中 `data-status="completed"` 的狀態文字。除此之外任何地方都不用綠——它不表示
  「通過」、「安全」或「新增」，只表示「這件事做完了」。

### Neutral
- **檯面紙底（Desk Paper）**（#eceae4，深色 #151517）：`body` 底色。整頁的第一層紙。
- **紙白（Sheet White）**（#ffffff，深色 #202024）：工作表。頁首、工作區、輸入框、對話框
  都落在這一層——「紙白＝正在工作的表面」是這個系統的空間語彙。
- **退紙（Recess Paper）**（#f2f0eb，深色 #26262b）：次一層的表面。按鈕 hover、停用
  底色、`.callout`、`.notice`、字幕線（`.cue-line`）、問答引用區塊，以及四階段流程中
  執行中的那一列。停用＝退到退紙。
- **墨（Ink）**（#1a1a1c，深色 #f2f1ed）：正文、標題與主要動作的實心底。對紙白 17.38:1。
- **次要墨（Muted Ink）**（#55555a，深色 #a9a8a2）：說明文字、狀態列、`.hint`、
  登錄列標籤、階段數字。對紙白 7.41:1，可長時間閱讀。
- **校對線（Rule）**（#c9c6bf，深色 #3a3a40）：欄與欄、列與列之間的 1px 分隔線。
- **格線（Rule Strong）**（#8a8781，深色 #6b6b73）：需要更強分隔時的線——區塊上緣
  （`.card`、`.registry`）、清單第一項、輸入框與按鈕邊界、校準十字與箭頭。比校對線深一階，
  因為它同時是「指標可以進去」的邊界。
- **墨黑主要動作（Primary Ink）**（#1a1a1c，深色 #f1f0ec）：`button.primary` 的底。
  Hover 走到 #000000（深色 #ffffff），配 `--color-primary-text`（#ffffff／深色 #17171a）。
- **選取底（Selection Wash）**（#e6d5cd，深色 #4a2c24）：`::selection` 的反白底色，
  配墨色文字。這是唯一帶色相的淺底，因為它站在紅鉛筆的家族裡。
- **灰階階梯（Greyscale Staircase）**（`--color-step-1` #1a1a1c 至 `--color-step-8` #ffffff）：
  八階中性階梯，無訊號工作表下緣那條參考帶的材料。它是**參考材質，不是主題**——深色模式
  刻意不覆寫它，量測基準不隨介面換裝。
- **遮幕（Backdrop）**（`rgb(26 26 28 / 55%)`，深色 `rgb(0 0 0 / 65%)`）：`dialog::backdrop`。
  對話框靠遮幕與原生置中出場，不靠陰影浮起。

### Named Rules
**The Marking-Pencil Rule（紅鉛筆規則）.** 紅只准出現在它標記的東西上：破壞、錯誤與失敗、
播放中字幕線的 IN／OUT 邊界、版本登錄列的目前位置記號、以及文字輸入的 caret。紅不是
強調色、不是裝飾底色、不是選取狀態——任何「安全」的動作（取消、關閉、重試）都不准沾紅。

**The Completion-Only Green Rule（綠只標完成規則）.** 版本綠只有一個意義：做完了。它標
`.done` 與 completed 狀態，不標「成功機率高」、「已通過驗證」或任何尚未發生的好事。

**The No-Blue Rule（無藍規則）.** 這個世界沒有藍色。主要動作是墨黑、選取是退紙、焦點環
是墨色；新表面不得引入任何藍色色相，包含連結、圖表與資訊型徽章。

## Typography

**介面字體：**系統字體堆疊，繁體中文優先：`-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif`
**等寬字體：**`ui-monospace, "SF Mono", Menlo, monospace`——這是量測字體，見下方規則。

**Character:** 系統堆疊是世界釘住的選擇：這個世界裡不存在展示體（display face）。紙張、
規則線與時間碼承擔全部性格，字體只承擔正確性——`PingFang TC` / `Noto Sans TC` 的原生
CJK 字形比任何網路字體都更正確、更快。行高 1.55 是為中英混排的長句設的；資料列改走 1.25，
讓同一欄的數字擠成可掃讀的一束。

### Hierarchy
- **H1（700, 1.5rem/24px, line-height 1.55）**：頁面唯一標題「YouTube 影片工作區」。
  全系統最大字級，一頁只有一個。
- **H2（700, 1.125rem/18px, line-height 1.55）**：區塊標題與影片標題（「開始處理」、
  「匯出影片」、「影片管理」）。影片卡片標題也升到這級。
- **H3（700, 1rem/16px, line-height 1.55）**：欄標題與分段標題（「匯入字幕」、「翻譯」）。
  與 H2 差一階級距，層級主要靠位置與欄位對齊表達。
- **H4（700, 0.9375rem/15px, line-height 1.55）**：卡片內的最後一層分段標題，與內文同級
  但加重，靠上緣 margin 分開。
- **Body（400, 0.9375rem/15px, line-height 1.55）**：說明段落、摘要、問答訊息。提示與
  空狀態限制在 `--measure`（68ch）內，不讓長句橫貫整個工作區。
- **Label（600, 0.9375rem/15px）**：表單標籤，上緣 `space-3`（12px）、下緣 `space-1`（4px）
  緊貼所屬欄位。核取方塊標籤降到 500 並改 flex 排列。
- **Small（400, 0.8125rem/13px）**：`.hint`、狀態列、登錄列的欄值、字幕版本清單的動作鈕。
  永遠配次要墨色。
- **Micro（700, 0.75rem/12px, letter-spacing 0.08em）**：登錄列的欄標籤（來源影音／原文字幕／
  目標字幕／成品）與字幕線的 IN／OUT 邊界。小、粗、開字距——它在紙上的位置像一格欄頭。
- **Mono（400, 0.8125rem/13px, line-height 1.25, tabular-nums）**：時間碼、時長、版本代號、
  解析度、階段百分比、問答引用摘要。見下方規則。

### Named Rules
**The Measurement-Only Mono Rule（等寬只量數規則）.** 等寬字體只用在真正的量測上：時間碼
（`00:12:04.5`）、時長、版本代號、解析度、階段數字、引用摘要。它不是「技術感」的裝飾，
也不准用來排內文、標籤或按鈕文字；凡是非量測的文字走系統堆疊。所有數字一律
`font-variant-numeric: tabular-nums`，讓同一欄的數字能上下對齊。

**The System-Stack Rule（系統堆疊規則）.** 這個系統沒有展示體，也不下載任何字體：正文、
標題、標籤全部走系統堆疊。若某個新區塊「需要更有個性的字」，答案是換欄位、換線或換密度，
不是引入字體。

**The 24px Ceiling Rule（24px 天花板規則）.** 任何文字不得超過 1.5rem（24px），且全頁只有
一個 H1。層級用字重、欄位位置與線表達，不用尺寸——超過 24px 的字不屬於這張表。

## Layout

三欄 CSS Grid 是整個產品：`minmax(13rem, 1fr)` 影片庫 ／ `minmax(22rem, 2.2fr)` 播放與字幕 ／
`minmax(17rem, 1.3fr)` 問答。比例由 `fr` 權重決定，因此播放欄永遠是視覺重心。

工作區是「一張紙」：`.workspace` 本身是紙白底、1px 校對線框、8px 圓角、`overflow: hidden`，
三欄之間只有一條 `border-left: 1px rule`。欄內距 `space-4`（16px）。頁首 `.app-header` 是
另一張紙白，內距 `space-3 space-4 space-4`，與工作區相隔 `space-4`。

區塊節奏靠「上緣一條格線 ＋ 上方外距」而不是卡片堆疊：`.card` 是
`border-top: 1px solid rule-strong; margin: var(--space-6) 0 0; padding-top: var(--space-3)`，
`.subform` 是同一手法的淺一階版本（`space-5` 外距、`space-2` 內距、校對線）。同一個區塊
可以一直往下長，不需要新的框。

兩個斷點，都服務「同一張表在不同寬度仍讀得出來」：

- **60rem（960px）以下**：Grid 轉單欄。影片庫在最上並可收合（`#library-toggle` 出現；
  `[data-collapsed="true"]` 時只留欄頭），播放與字幕欄接著，問答欄最後；
  `.narrow-tabs` 的 tablist 在此出現，讓「字幕／問答」共用一個面板而不卸載播放器或未送出的
  草稿。欄與欄的分隔線從左側改到底部。版本登錄列同時從 4 欄改為 2 欄，紅記號寬度改為 50%
  並重新對應位移，讓它跟著目前欄位換行而不是滑出邊界。
- **30rem（480px）以下**：`body` 內距收到 `space-2`（8px），頁首與欄內距收到 `space-3`
  （12px），登錄欄內距收到 `space-2`，網址列按鈕撐滿寬度，對話框內距收緊。這是給 200%
  縮放與窄視窗用的，不是手機版。

頁首的 `.app-bar` 是 flex 列，標題與服務狀態以 `baseline` 對齊並分置兩端，可換行。
`.skip-link` 平常位於視窗上方之外（`top: -5rem`），取得焦點時滑入至 `space-4`。

**The Three-Station Rule（三工位規則）.** 桌面寬度下，三個工作區必須同時可見、可直接操作；
任何功能不得被藏進只顯示單一欄的模式裡。60rem 以下換的是排列方式，不是把工位收起來——
tab 換面板時播放器與未送出的草稿都留在原處。

## Elevation & Depth

**這個系統完全沒有陰影，也沒有浮起的表面。** 全檔 CSS 只有一個 `box-shadow`，而且它不是
高程：`.worksheet::after` 用它複製兩份格線色的橫線來畫無訊號校準十字的垂直兩臂
（`0 ∓3.5rem 0 0 rule-strong`，零模糊的硬位移），是繪圖手法，不是表面語言。任何新表面
都不得引用它當陰影範本。

深度一律由三件事表達：（一）明度差——檯面紙底 #eceae4 上放紙白 #ffffff，需要更低調時
退到退紙 #f2f0eb；（二）1px 的校對線與格線；（三）欄位對齊本身，也就是說「同一件事的
資料落在同一欄」就是層次。對話框也不靠陰影浮起：它靠 `<dialog>` 的原生置中、
`rgb(26 26 28 / 55%)` 的遮幕與 1px 格線框把自己從背景切出來。

動態只存在於狀態，不存在於裝飾：按鈕在背景與邊框色上換色（`--duration-state` .14s、
`--ease-state` ease-out），版本登錄列的紅記號滑到目前欄（`--duration-settle` .42s、
`--ease-settle` cubic-bezier(.16, 1, .3, 1)；首次繪製時以 `.instant` 取消轉場），且
`prefers-reduced-motion: reduce` 下紅記號的轉場整個關閉。全檔沒有 `@keyframes`、沒有
`animation`、沒有 `filter`。

### Named Rules
**The Ink-Line Rule（線即深度規則）.** 需要分隔感時加一條線或調整紙的明度，不要加陰影、
模糊或玻璃。深度是線，不是光；唯一的 `box-shadow` 是畫校準十字的筆，不是可繼承的表面樣式。

## Shapes

形狀語言是「紙上的矩形」：兩個矩形圓角加一個真圓。

- **容器 8px**（`--radius`）：`.app-header`、`.workspace`、`dialog`、`.player-empty`、`video`。
- **控制項 6px**（`--radius-small`）：按鈕、輸入框、下拉、文字區、`.callout`、`.notice`、
  `.cue-line`、影片庫項目，以及收合分頁的上緣。
- **真圓 50%**（`--radius-round`）：只給畫出來的圓——狀態記號的圓環與圓點、無訊號工作表的
  校準環。它不是第三個「圓角值」，因為它從不出現在矩形表面上。

邊框一律 1px，在 `#c9c6bf`（校對線）與 `#8a8781`（格線）之間二選一；沒有漸層邊框、沒有
膠囊、沒有切角。兩個非對稱輪廓都在同一個邏輯上——「上緣貼齊它所屬的線」：`.notice` 是
`0 0 6px 6px`（上緣貼住那條 2px 墨色線），窄版的 `.narrow-tabs` 分頁是 `6px 6px 0 0`
（下緣貼住面板邊界）。

有兩個形狀不屬於上面任何一類，因為它們不是表面：`.cue-arrow` 的帶頭規則線與
`.panel-caret` 的收合箭頭。兩者共用同一個畫法——**兩個邊的邊框加一次旋轉**
（`.cue-arrow::after` 留上與右、`.panel-caret` 留下與右，都轉 45°），只吃
`--mark-stroke` 的粗細，不帶圓角、也不佔一張面。它們與狀態記號共用同一條紀律：
形狀要畫出來，不用字符。

**The Two-Radius Rule（雙圓角規則）.** 除了 8px 與 6px，不准出現第三個矩形圓角值；圓形
一律用 `--radius-round` 畫，不准用大圓角假裝。若某個新元素「感覺需要」4px 或 12px，
那是它在要求被重新歸類，不是要求新數值。

## Components

### Buttons
- **Shape:** 6px 圓角、1px 格線邊框（顏色隨變體）、`8px` 四邊內距、與內文同字級。
- **Primary（墨黑實心）:** `--color-primary` 底（＝墨）、`--color-primary-text` 字，邊框與
  底色同色，600 字重，用於一屏一個的主要動作（載入影片、開始處理、送出問題）。
  Hover 走到 `--color-primary-hover`（#000000／深色 #ffffff）。
- **Default:** 紙白底墨字。Hover 時整顆沉入退紙 #f2f0eb、邊框轉墨色——只換色，不改幾何。
  取消、重新查詢、次要操作都用它。
- **Danger:** 兩種強度。`button.danger` 是紙白底紅字紅框的觸發鈕（「刪除整支影片…」）；
  `#delete-confirm` 是唯一允許紅底白字的按鈕，且必須勾選確認後才啟用。紅在整個產品裡的
  面積就只有這些。
- **Selected:** `aria-pressed="true"` 與影片庫的 `aria-current="true"` 共用同一套外觀：
  退紙底 ＋ 墨色框 ＋ 600 字重。選取是「被壓下去的一格」，不是彩色高亮。
- **Disabled:** 底退到退紙、字退到次要墨、框退到校對線、游標 `not-allowed`。
- **Focus:** 所有元素共用同一個焦點環——`2px` 實線墨色（`--color-focus`）＋ `2px` 外距
  （`outline-offset`）。鍵盤使用者看到的輪廓不因元件而異。

### Cards / Containers
- **Corner Style:** 沒有圓角、沒有底色、沒有框。`.card` 是一條上緣格線加內距的規則區塊
  （`border-top: 1px solid rule-strong`、上距 `space-6`、內距上緣 `space-3`）。
- **Background:** 透明——它印在工作表上，不是貼在表上。
- **Shadow Strategy:** 無。見 Elevation & Depth。
- **Internal Padding:** 區塊內距走 `space-3`／`space-4` 的節奏；區塊之間靠 `space-6` 上距
  與那條格線分開。區塊不嵌套區塊：區塊內的分段改用 `.subform` 的淺一階線。

### Folded Blocks（收合區塊）
第二層的區塊是 `<details class="card panel">`：收合時只留標題行，展開時在同一個格線區塊
下攤開全部欄位。上緣那條格線同時是把手，所以收合不會讓區塊從紙上消失——**收合的是內容，
不是「這個區塊存在」的事實**。

summary 是一條三欄格線（`1em minmax(0, 1fr) auto`、`align-items: baseline`、
`gap: space-2`）：左欄是畫出來的 `.panel-caret`，中欄放區塊自己的 `<h3>`（因此
`.panel > summary > h3` 必須 `margin: 0`，否則標題會在格線下多推一次上距），右欄是狀態。
`list-style: none` 加上 `::-webkit-details-marker { display: none }` 關掉瀏覽器內建的三角形。

- **Caret（畫出來的）:** `.panel-caret` 是 `.5em` 見方、`--mark-stroke` 粗細的兩個邊
  （`border-width: 0 1px 1px 0`）轉 45°，展開時轉到 -135°。它不是字符，也不是三角形——
  與 cue 行的帶頭箭頭同一套畫法。轉動只走 `--duration-state`／`--ease-state`，並與登錄列
  的紅記號一起在 `prefers-reduced-motion: reduce` 下關閉。
- **`.panel-state`:** 右欄以次要墨與 Small 字級交代這個區塊裡有什麼——字幕版本數、成品數、
  工作狀態。**收合後它是唯一還留在紙上的資訊**，所以它必須是數量或狀態，不能是裝飾。
  它走系統字堆疊，不穿等寬：這裡讀的是「有幾個」，不是量測值。（真正是量測的解析度走
  `.num`，見下面 `.choices`。）
- **`.panel-hint`:** 只有流程選擇那一行用（「調整」），hover 時由次要墨轉墨色：同一個位置
  換成「這裡可以動」的指示。它與 `.panel-state` 共用次要墨與 Small 字級，靠右對齊。
- **`.choices`（流程選擇行）:** `#flow-choices` 是唯一不收 `.card` 的收合區塊，它住在
  `<form id="processing-form">` 裡，收合時由 `#flow-choices-values` 讀出四個選擇
  （解析度、目標語系、單語／雙語、字幕形式），讓「四個都採用預設」這個常見情況一次互動
  都不需要。解析度是量測，走 `.num`（墨色）；其餘是散文。
- **一次只記一支影片:** 收合狀態依影片記在 `localStorage`（`vcc.panels.v1:<id>`），切換
  影片不互相汙染。`#jobs-panel` 是唯一會自己動的區塊：只有真的有待處理或執行中的工作時
  自動展開，完成後收回——正在跑的事不該讓人去把它找出來。

### Inputs / Fields
- **Style:** 紙白底、1px 格線邊框、6px 圓角、`8px` 內距，寬度撐滿所屬欄位。核取方塊例外
  ——`width: auto`，以 `accent-color` 吃墨色。
- **Caret:** 紅（`caret-color: var(--color-mark)`）。這是紅鉛筆在工作表上的落點。
- **Focus:** 與其他元素相同的 2px 墨色焦點環，不另外變更邊框色。
- **Error:** 不變更欄位本身，改以 `.error-text`（紅、600 字重）出現在欄位附近；空字串時
  整個隱藏（`.error-text:empty { display: none }`）。
- **Disabled:** 底、字、框三者同時退場（退紙／次要墨／校對線），讓「不能動」一眼可辨。

### Navigation
沒有導覽列。頁首 `.app-header` 是紙白加 1px 校對線框與 8px 圓角，同時扮演標題列、匯入
表單與版本登錄列的容器。真正的「導覽」是 `.narrow-tabs`：只在 60rem 以下出現的水平
tablist，選取中的分頁為退紙底＋墨色框＋2px 墨色下緣＋600 字重，未選取者維持紙白。
桌面寬度不使用分頁——三欄同時可見。

### Version Register（signature）
頁首下方一條全寬、四欄的登錄列（`.registry` 內 `dl.registry-fields`，欄位
`data-field="source|original|target|artifact"`）。每欄是 `<dt>` 的 Micro 標籤（來源影音／
原文字幕／目標字幕／成品，次要墨、開字距）加上 `<dd>` 的 Small 值（行高 1.25），
欄間一條 1px 校對線。**目前做到哪由一條 2px 紅記號標出**：`#registry-mark` 是絕對定位的
橫條，高度 `--rule-mark`（2px）、寬度 25%，`background: var(--color-mark)`，依
`data-current="original|target|artifact"` 以 `translateX` 位移 100%／200%／300%；沒有
`data-current` 時整條隱藏（`display: none`），所以「現在沒有進行中的事」不會假裝有一個位置。
它是整個世界唯一的簽名動作，也是頁首唯一的紅。60rem 以下欄位改 2 欄、記號改 50% 寬並
重新對應位移，確保記號永遠對齊目前欄位且留在紙內。深色模式 #ff9b85。

### Playing Cue Line（signature）
播放器下方一條退紙底的等寬行（`.cue-line`），讀出播放中的 VTT 字幕：
`IN 00:12:04.5 ▸ 00:12:07.2 OUT`。`IN`／`OUT` 是 `.cue-edge`——Micro、700、開字距、紅；
時間碼是 `.cue-time`（等寬、tabular）；中間的分隔是 `.cue-arrow`，一條**畫出來的**帶頭
規則線（`--arrow-length` 1rem，格線色），不是字符。行末 `.cue-state` 用次要墨說明狀態，
`data-state` 分為 `idle`（未選擇）、`off`（目前不能跳播／尚未載入）、`none`（本段無字幕）、
`live`（正在播）；`off` 與 `none` 下時間碼退回次要墨，讓「沒有數值」和「有數值」在顏色上
就分得開。

### No-Signal Worksheet（signature）
播放器沒有來源時不給空白灰塊，給一張畫出來的對位工作表（`.player-empty`，16/9、
退紙底、1px 校對線框、8px 圓角）。下緣是 8 階灰階階梯（`.worksheet::before`，高度
`--strip-height` 2.25rem、上緣 1px 格線、以 `linear-gradient` 一次畫出 `--color-step-1..8`
各 12.5%）；中央是校準環（`.reticle`，直徑 `--reticle-ring` 2.75rem、1px 格線圓、
退紙底）與十字（`.worksheet::after`，寬 `--reticle-cross` 7rem）。說明文字走等寬、Small、
次要墨，墊在階梯之上。全部由 CSS 漸層與邊框畫成——`url(` 在這個專案是禁止的，所以沒有
任何 SVG data-URI。

### Status Marks（signature）
狀態記號是**畫出來的形狀，不是字符**：`.mark` 是 `1.1em` 寬的 inline 區塊，用
`::before`／`::after` 描出形狀，`currentColor` 上色，因此記號永遠不會自帶色相。形狀對應
`span.mark[data-mark]`：等待是髮絲圓環、執行中是圓環加實心點、完成是打勾、停止是叉、
暫停是兩條直槓、資訊是實心點。全部共用一個 `--mark-stroke`（1px）與一個 `--mark-size`
（0.625rem）。記號一律 `aria-hidden="true"` 的裝飾，語意由緊鄰的繁體中文文字承擔，顏色
從來不是唯一訊號。已知的窄處：`failed` 與 `cancelled` 共用同一個叉，`pending` 與 `queued`
共用同一個圓環，因此每一對的差別只在緊鄰的可見文字標籤上。

### Video Library Item
影片庫是 `list-style: none` 的清單，每一項是整行寬的透明按鈕（邊框透明、左對齊、6px 圓角）。
標題之下疊一行等寬、tabular 的次要墨中繼資料（時長 · 位置）。目前選取的項目套上退紙底、
墨色框與 600 字重。它看起來像清單，實際上是按鈕——這是此產品「每樣東西都能直接動手」的
縮影。清單項之間是 1px 校對線，第一項改用格線。

### Notice / Callout
低調的區塊用退紙把話說完，不靠顏色。`.notice`（例如 Gemini 未設定）是退紙底、
**上緣 2px 墨色線**、下緣圓角 `0 0 6px 6px`——它貼在所屬區塊的下緣，像一張釘在紙邊的
便條，存在感低於任何紅色。`.callout`（例如預覽狀態）是退紙底、1px 校對線框、6px 圓角、
`space-2 space-3` 內距，用於欄位群組內的補充說明。兩者都不使用彩色。

### QA Message with Citation
問答訊息是底部一條 1px 校對線的 `<ol>` 項目（`padding: space-3 0`）。引用字幕處使用
`<blockquote>`：退紙底、1px 校對線框、6px 圓角、`white-space: pre-wrap` 保留模型回覆的
換行，並限制在 `--measure` 內。引用的 `<summary>` 是等寬 tabular 的 Small（引用起訖時間
與依據版本代號），模型補充知識放在 `<details>` 內收合，與影片內容的引用在結構上就分開。

### Delete Dialog（signature）
`<dialog>` 原生模態、紙白底、1px 格線框、8px 圓角、`16px 24px` 內距，寬度
`min(32rem, calc(100vw - 2rem))`，超過視窗高度時自身捲動。刪除範圍先計算並列出，再要求
勾選「我了解會永久刪除…」才能啟用紅色「永久刪除」。取消鍵維持預設樣式，永遠與紅色並排
但永不被紅色污染。

## Do's and Don'ts

### Do:
- **Do** 讓紙的三層明度承擔空間感：檯面 #eceae4、工作表 #ffffff、退紙 #f2f0eb。需要更低調
  就用退紙，不要把顏色當成「次要表面」的解法。
- **Do** 需要分隔時先畫一條線：區塊上緣用格線（#8a8781），欄與列用校對線（#c9c6bf）。
- **Do** 讓主要動作穿墨黑實心（`--color-primary`），一屏一個；選取狀態用退紙底＋墨框＋
  600 字重，不用彩色。
- **Do** 為每個可互動元素保留 2px 墨色焦點環與 2px 外距；`outline-offset` 是它不被邊框
  吃掉的原因。
- **Do** 把紅（#a8341f）只用在破壞、失敗、播放中字幕線的 IN／OUT 邊界、登錄列的目前位置
  記號與 caret，把綠（#2f6f4f）只用在完成與 `.done`——兩者的清單都是封閉的。
- **Do** 用等寬字體（tabular-nums）排時間碼、時長、版本代號、解析度與階段數字，其餘文字
  一律系統堆疊。
- **Do** 新增元素前先從既有的兩個矩形圓角（8px 容器、6px 控制項）選一個；需要畫圓就用
  `--radius-round`。
- **Do** 狀態一律「畫出來的記號 ＋ 繁體中文文字」成對出現，記號用 `currentColor` 上色並
  標 `aria-hidden`，讓顏色不是唯一訊號。
- **Do** 讓按鈕文字說出它會做的事（「取得原文字幕」、「依此摘要匯出影片」），錯誤訊息說出
  問題與恢復方式；介面文字維持繁體中文。
- **Do** 收合區塊的標題行自己交代數量或狀態（`.panel-state`）；收合之後那行是唯一還留在
  紙上的資訊，別讓它只剩一個標題。
- **Do** 讓深淺兩套 token 都跟隨系統偏好，並在深色模式量測對比（現況：內文 6.32–16.14:1，
  畫出來的記號 7.69–14.36:1）。

### Don't:
- **Don't** 加任何表面陰影、模糊或玻璃。唯一的 `box-shadow` 是畫校準十字垂直兩臂的筆
  （`.worksheet::after`），不得被當成高程樣式繼承。
- **Don't** 引入藍色，或把紅／綠當成「強調色」使用；紅與綠各自只有封閉的一份用途清單。
- **Don't** 用字符或 emoji 當狀態圖示或收合指示——記號要畫出來（圓環、打勾、叉、雙槓、
  圓點），收合箭頭也要（`.panel-caret`），且都不得自帶色相。
- **Don't** 把等寬字體用在內文、標籤或按鈕文字上；它不是風格，是量測工具。
- **Don't** 新增第三個矩形圓角值、膠囊造型或漸層邊框。
- **Don't** 使用 1.5rem 以上的字級，也不要用佔位文字取代標籤。
- **Don't** 讓登錄列的紅記號在沒有 `data-current` 時露面——沒有進行中的工作就不該有記號。
- **Don't** 在 60rem 以上把三欄收成單欄或分頁；同時可見是這張表的前提。
- **Don't** 讓動態超出狀態：沒有 `@keyframes`、沒有裝飾性動畫，紅記號的滑動與收合箭頭的
  轉動在 `prefers-reduced-motion: reduce` 下都必須關閉。
- **Don't** 直接顯示例外訊息原文；錯誤一律使用產品自己的語言，並保留已完成的資產。
