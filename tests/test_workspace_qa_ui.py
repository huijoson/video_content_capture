"""Offline contracts for the Q&A page's accessible, safe controls."""

from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).parents[1] / "src/video_content_capture/workspace/static"


class Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, dict[str, str | None]] = {}
        self.labels: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids[str(attributes["id"])] = {"tag": tag, **attributes}
        if tag == "label" and attributes.get("for"):
            self.labels.add(str(attributes["for"]))


def test_qa_controls_have_labels_disabled_submission_and_subtitle_entry_points() -> None:
    page = Controls()
    html = (STATIC / "index.html").read_text()
    page.feed(html)
    for control in ("qa-source", "conversation-select", "question"):
        assert control in page.ids
        assert control in page.labels
    assert "disabled" in page.ids["qa-submit"]
    assert page.ids["qa-status"]["aria-live"] == "polite"
    assert page.ids["qa-form"]["tag"] == "form"
    assert 'href="#acquire-subtitles"' in html
    assert 'href="#subtitle-import-form"' in html
    assert "GEMINI_API_KEY" in html


def test_qa_script_preserves_source_citations_and_has_no_streaming_or_focus_theft() -> None:
    script = (STATIC / "workspace.js").read_text()
    for text in (
        "已建立新對話",
        "影片內容",
        "補充知識（未經網路查證）",
        "缺乏依據",
        "本次參考最近",
        "不在記憶內",
        "目前不能跳播",
        "/conversations",
        "/messages",
    ):
        assert text in script
    assert 'event.key === "Enter"' in script
    assert "event.isComposing" in script
    # Late responses for another video/conversation are never rendered into the current one.
    assert "conversation.video_id !== currentVideo?.id" in script
    assert "generation !== qaGeneration" in script
    assert "innerHTML" not in script
    assert ".focus(" not in script
    assert "ReadableStream" not in script


def test_citation_jump_uses_pinned_version_and_keeps_playback_subtitles() -> None:
    script = (STATIC / "workspace.js").read_text()
    start = script.index("function renderCitation(")
    body = script[start : script.index("\nfunction ", start + 1)]
    # Citations show the saved text/time of the conversation's own version, never the current one.
    assert "citation.text" in body
    assert "player.currentTime = citation.start" in body
    assert "details.open = true" in body
    assert "track" not in body
    assert "currentVideo.qa_version_id" not in body
    assert "/track.vtt" not in body
    # A claim's evidence folds to one line; a lone quote is not wrapped in a group of one.
    group_start = script.index("function renderCitationGroup(")
    group = script[group_start : script.index("\nfunction ", group_start + 1)]
    assert "renderCitation(" in group
    assert "ordered.length === 1" in group
    assert "details.append(renderCitation(citation, conversation));" in group
    # The answer states its version once, and keeps the memory count on that same line.
    provenance_start = script.index("function renderProvenance(")
    provenance = script[provenance_start : script.index("\nfunction ", provenance_start + 1)]
    assert "conversation.source_version_id" in provenance
    assert "message.memory_ids" in provenance
    message = script[script.index("function renderQaMessage(") :]
    assert "message.in_memory" in message


def test_the_conversation_scrolls_while_the_composer_keeps_its_place() -> None:
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "workspace.css").read_text()
    rules = css[css.index("/* end tokens */") :]
    # The messages are the pane's only scroll region, and the composer sits after them.
    assert html.index('id="qa-messages"') < html.index('id="qa-form"')
    assert 'id="qa-messages" aria-label="問答訊息" tabindex="0"' in html
    box = rules[rules.index("#qa-messages {") : rules.index("#qa-messages > li")]
    assert "overflow-y: auto" in box
    assert "overscroll-behavior: contain" in box
    # Browser surfaces belong to the palette: the scroller is a thin rule on recess paper.
    assert "scrollbar-color: var(--color-rule-strong) var(--color-recess)" in box
    assert "scrollbar-width: thin" in box
    pane = rules[rules.index("#qa-pane {") : rules.index("#qa-pane > *")]
    assert "display: flex" in pane and "flex-direction: column" in pane
    assert "height: calc(100dvh - var(--shell-band))" in pane
    # A new question and its answer keep the foot of the log in view, and scrolling up is respected.
    script = (STATIC / "workspace.js").read_text()
    assert "function followNewestQaMessage(" in script
    assert "qaFollowNewest = true" in script
    assert 'element("qa-messages").addEventListener("scroll"' in script


def test_the_qa_picker_folds_to_one_row_and_the_answer_keeps_only_its_evidence() -> None:
    html = (STATIC / "index.html").read_text()
    script = (STATIC / "workspace.js").read_text()
    # 問答依據, 設為問答依據, 已保存的對話 and 刪除此串對話 all live behind one folded row
    # whose readback is the conversation's own source version.
    setup = html[html.index('id="qa-setup"') : html.index('id="qa-no-source"')]
    assert 'id="qa-setup" class="card panel"' in html
    for control in (
        "qa-source",
        "qa-new-conversation",
        "conversation-select",
        "qa-delete-conversation",
    ):
        assert f'id="{control}"' in setup
    assert '<span id="qa-source-summary">' in setup
    assert 'setup: "qa-setup"' in script
    assert 'if (name === "setup") return !element("qa-no-source").hidden;' in script
    # An answer renders only the sections that hold something, folds each claim's quotes to one
    # line, and states its version once.
    assert "本次無此類回覆" not in script
    assert "if (!entries.length) continue;" in script
    assert "section.append(renderCitationGroup(entry.citations, conversation));" in script
    assert "本次參考最近 ${remembered} 組問答" in script
