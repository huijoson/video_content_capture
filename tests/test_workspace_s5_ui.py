"""S5 offline UI contracts: labels, required states, local-only assets, central CSS tokens."""

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).parents[1] / "src/video_content_capture/workspace/static"


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, dict[str, str | None]] = {}
        self.label_for: set[str] = set()
        self.controls: list[tuple[str, dict[str, str | None], bool]] = []
        self.label_depth = 0
        self.resources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids[str(attributes["id"])] = {"tag": tag, **attributes}
        if tag == "label":
            self.label_depth += 1
            if attributes.get("for"):
                self.label_for.add(str(attributes["for"]))
        if tag in {"input", "select", "textarea"}:
            self.controls.append((tag, attributes, self.label_depth > 0))
        for name in ("src", "href"):
            if attributes.get(name):
                self.resources.append(str(attributes[name]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self.label_depth -= 1


def page() -> tuple[Page, str]:
    html = (STATIC / "index.html").read_text()
    parsed = Page()
    parsed.feed(html)
    return parsed, html


def test_every_input_has_a_visible_label() -> None:
    parsed, _ = page()
    assert parsed.controls
    for tag, attributes, wrapped in parsed.controls:
        assert wrapped or attributes.get("id") in parsed.label_for, (tag, attributes)
        assert "aria-label" not in attributes or wrapped or attributes["id"] in parsed.label_for


def test_three_column_regions_and_cleanup_controls_exist() -> None:
    parsed, _ = page()
    for element_id in (
        "library-heading",
        "playback-heading",
        "qa-heading",
        "library-search",
        "make-preview",
        "clear-preview",
        "delete-video",
        "delete-dialog",
        "delete-scope",
        "delete-acknowledge",
        "delete-confirm",
        "delete-cancel",
        "pending-deletions",
        "cancel-query",
        "preview-status",
    ):
        assert element_id in parsed.ids, element_id
    assert parsed.ids["delete-dialog"]["tag"] == "dialog"
    assert parsed.ids["delete-dialog"]["aria-labelledby"]
    assert "disabled" in parsed.ids["delete-confirm"]
    assert parsed.ids["preview-status"]["aria-live"] == "polite"


def test_status_table_rows_have_text_in_page_or_script() -> None:
    _, html = page()
    text = html + (STATIC / "workspace.js").read_text()
    states = {
        "empty library": ["影片庫目前是空的", "貼上"],
        "loading": ["正在查詢影片資料", "取消查詢"],
        "running": ["進度總量未知", "處理中"],
        "failed": ["失敗", "已保留", "重試"],
        "gemini missing": ["GEMINI_API_KEY", "重新啟動服務"],
        "no qa source": ["取得字幕後可開始問答"],
        "preview failed": ["相容預覽製作失敗", "目前不能跳播", "成品下載"],
        "pending cleanup": ["待清理", "重試刪除"],
    }
    for state, phrases in states.items():
        for phrase in phrases:
            assert phrase in text, (state, phrase)


def test_status_uses_icons_with_text_not_color_alone() -> None:
    script = (STATIC / "workspace.js").read_text()
    assert "statusIcons" in script
    for status in ("queued", "running", "completed", "failed", "cancelled", "interrupted"):
        assert re.search(rf"{status}: \"[^\"]+\"", script)


def test_only_local_resources_and_no_external_urls() -> None:
    parsed, html = page()
    for resource in parsed.resources:
        assert not re.match(r"[a-z]+:", resource) or resource.startswith("#"), resource
        assert not resource.startswith("//"), resource
    for name in ("index.html", "workspace.css", "workspace.js", "dialog.js"):
        content = (STATIC / name).read_text()
        assert not re.search(r"https?://", content), name
        assert "@import" not in content
        assert "url(" not in content or name != "workspace.css"
    assert '<script src="/static/dialog.js" defer></script>' in html


def test_css_tokens_are_central_and_dark_mode_keeps_tokens() -> None:
    css = (STATIC / "workspace.css").read_text()
    end = css.index("/* end tokens */")
    tokens, rules = css[:end], css[end:]
    for token in ("--color-", "--space-", "--font-size-", "--radius"):
        assert token in tokens, token
    assert "prefers-color-scheme: dark" in tokens
    # Colors and raw sizes for theming live only in the token block.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules)
    assert not re.search(r"\brgba?\(|\bhsla?\(", rules)
    assert "@media (max-width" in rules


def test_keyboard_dialog_behaviour_and_no_space_interception() -> None:
    script = (STATIC / "workspace.js").read_text()
    dialog = (STATIC / "dialog.js").read_text()
    assert "showModal" in dialog
    assert '"Tab"' in dialog
    assert '"cancel"' in dialog  # native Esc on a modal dialog fires cancel
    assert ".focus(" in dialog  # focus returns to the opener after close
    assert "opener" in dialog
    # The main script never moves focus on its own (chat updates do not steal focus).
    assert ".focus(" not in script
    for needle in ('" "', '"Space"', "event.code"):
        assert needle not in script
    assert "aria-current" in script


def test_narrow_layout_uses_subtitle_and_qa_tabs_with_collapsible_library() -> None:
    # Spec section 3: narrow windows show the player on top, 字幕／問答 tabs below, library folded.
    parsed, html = page()
    tablist = parsed.ids["narrow-tabs"]
    assert tablist["role"] == "tablist"
    for tab_id, panel in (("tab-subtitles", "subtitle-panels"), ("tab-qa", "qa-pane")):
        tab = parsed.ids[tab_id]
        assert tab["tag"] == "button" and tab["role"] == "tab"
        assert tab["aria-controls"] == panel and panel in parsed.ids
    assert parsed.ids["library-toggle"]["aria-expanded"] in {"true", "false"}
    css = (STATIC / "workspace.css").read_text()
    narrow = css[css.index("@media (max-width: 60rem)") :]
    assert '[data-narrow-tab="qa"] #subtitle-panels' in narrow
    assert '[data-narrow-tab="subtitles"] #qa-pane' in narrow
    assert '.library[data-collapsed="true"]' in narrow
    # User-initiated focus moves live apart from workspace.js, which must never move focus.
    assert "/static/narrow-tabs.js" in html
    script = (STATIC / "narrow-tabs.js").read_text()
    assert "ArrowRight" in script and "aria-selected" in script
