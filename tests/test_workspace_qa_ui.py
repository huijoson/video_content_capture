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
    assert "conversation.source_version_id" in body
    assert "citation.text" in body
    assert "player.currentTime = citation.start" in body
    assert "details.open = true" in body
    assert "track" not in body
    assert "currentVideo.qa_version_id" not in body
    assert "/track.vtt" not in body
    message = script[script.index("function renderQaMessage(") :]
    assert "message.memory_ids" in message
    assert "message.in_memory" in message
