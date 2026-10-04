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
        "format",
        "audio",
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
