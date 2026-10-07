"""Offline contracts for the progressively enhanced workspace page."""

from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).parents[1] / "src/video_content_capture/workspace/static"


class Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.by_id[element_id] = {"tag": tag, **attributes}


def test_import_selection_and_job_controls_have_accessible_elements() -> None:
    page = Elements()
    page.feed((STATIC / "index.html").read_text())
    for element_id in (
        "load-video",
        "refresh-source",
        "resolution",
        "resolved-source",
        "audio-summary",
        "start-job",
        "media-asset",
        "player",
    ):
        assert element_id in page.by_id
    assert "disabled" not in page.by_id["load-video"]
    assert "controls" in page.by_id["player"]
    assert page.by_id["job-status"]["aria-live"] == "polite"
    assert page.by_id["job-progress"]["tag"] == "progress"
    for element_id in ("subtitle-language", "include-original", "question"):
        assert "disabled" in page.by_id[element_id]
    # Quality is chosen by resolution only; the audio track is shown read-only.
    assert page.by_id["resolution"]["tag"] == "select"
    assert page.by_id["audio-summary"]["tag"] == "p"
    assert "format" not in page.by_id and "audio" not in page.by_id


def test_quality_menus_list_only_resolutions() -> None:
    script = (STATIC / "workspace.js").read_text()
    assert "metadata.resolutions" in script
    assert "default_resolution" in script
    assert "metadata.formats" not in script and "metadata.audio_tracks" not in script
    assert "format_id: element(" not in script
    # Re-export picks a saved asset labelled by its resolution only.
    assert "`格式 ${asset.format_id}" not in script


def test_browser_script_uses_controlled_ids_and_safe_text() -> None:
    script = (STATIC / "workspace.js").read_text()
    for endpoint in ("/api/query", "/api/jobs", "/cancel", "/retry", "/position", "/media"):
        assert endpoint in script
    assert "innerHTML" not in script
    assert "textContent" in script
    assert 'credentials: "same-origin"' in script


def test_s3_subtitles_translation_and_confirmed_export_controls() -> None:
    page = Elements()
    page.feed((STATIC / "index.html").read_text())
    for element_id in (
        "subtitle-list",
        "subtitle-file",
        "import-language",
        "version-name",
        "import-subtitles",
        "acquire-subtitles",
        "translation-source",
        "translate",
        "retranslate",
        "export-target",
        "export-original",
        "export-preview",
        "export-summary",
        "export-start",
        "export-list",
    ):
        assert element_id in page.by_id
    assert "checked" not in page.by_id["include-original"]
    assert "disabled" in page.by_id["question"]
    script = (STATIC / "workspace.js").read_text()
    assert 'document.createElement("track")' in script
    assert "track.vtt" in script
    assert '"translation-source"' in script
    assert '"export-selection"' in script
    assert "/exports/preview" in script
    assert "innerHTML" not in script


def test_one_click_flow_confirm_screen_controls() -> None:
    page = Elements()
    page.feed((STATIC / "index.html").read_text())
    for element_id in (
        "flow-summary",
        "flow-language",
        "flow-include-original",
        "flow-subtitle-form",
        "flow-facts",
        "flow-status",
        "flow-stages",
        "flow-artifact",
    ):
        assert element_id in page.by_id
    assert page.by_id["flow-language"]["tag"] == "select"
    assert page.by_id["flow-subtitle-form"]["tag"] == "select"
    assert page.by_id["flow-include-original"]["tag"] == "input"
    assert page.by_id["flow-status"]["aria-live"] == "polite"
    assert page.by_id["flow-blocked"]["aria-live"] == "polite"
    # The flow summary and the read-only facts are announced without stealing focus.
    assert page.by_id["flow-facts"]["tag"] == "ul"
    # The one-click flow burns subtitles by default (ADR 0004).
    script = (STATIC / "workspace.js").read_text()
    assert 'value="burned" selected' in (STATIC / "index.html").read_text()
    # The confirm screen is fed by the read-only flow endpoint and the frozen start endpoint.
    assert "/flow?" in script and "/flows" in script
    # The chosen subtitle form is sent as-is; the download helper stays for plain downloads.
    assert 'subtitle_form: element("flow-subtitle-form").value' in script
    # The 「原文＋目標」 control is disabled whenever a translation cannot happen.
    assert "bilingual_allowed" in script
    # A flow always targets a real language; the backend cannot represent 「不翻譯」,
    # so the menu must not offer an empty choice the start request would have to fake.
    assert "不翻譯" not in script
    assert "不翻譯" not in (STATIC / "index.html").read_text()
    assert 'target_language: element("flow-language").value,' in script
    assert "flowConfirm?.original_language" not in script
    # The target menu offers the documented first batch and everything the source already
    # carries; the backend accepts any valid tag, so this menu is the only limit.
    for language in ('"zh-TW"', '"zh-CN"', '"en"', '"ja"', '"ko"', '"es"', '"fr"', '"de"'):
        assert language in script
    assert 'options(element("flow-language"), flowLanguageChoices(video)' in script
    # The translation panel and the flow menu share one list, so they cannot drift apart.
    assert script.count("flowLanguageChoices(") >= 3
    # Progress and the finished artifact come from the flow status surface.
    assert "flow-artifact" in script and "下載影片" in script
    assert "/api/exports/${controlled(payload.artifact.id)}/download" in script


def test_one_click_flow_exposes_cancel_and_retry_controls() -> None:
    markup = (STATIC / "index.html").read_text()
    page = Elements()
    page.feed(markup)
    for element_id in ("flow-actions", "cancel-flow", "retry-flow"):
        assert element_id in page.by_id
    assert page.by_id["cancel-flow"]["tag"] == "button"
    assert page.by_id["retry-flow"]["tag"] == "button"
    # Both controls start hidden; the panel reveals only the one that applies.
    assert "hidden" in page.by_id["flow-actions"]
    assert "取消流程" in markup and "重試流程" in markup
    script = (STATIC / "workspace.js").read_text()
    assert "需手動重試" in script
    assert '"/cancel"' in script and '"/retry"' in script
    assert "/api/flows/${controlled(flowId)}" in script
    # A reloaded page adopts the video's latest flow so an interrupted one is visible.
    assert "latest_flow_id" in script
    assert "innerHTML" not in script
