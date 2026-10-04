"""Offline translation boundary and validation contracts."""

import json

import pytest

from video_content_capture.workspace.translation import TranslationError, validate_translation


def test_translation_keeps_exact_ids_and_rejects_provider_failures():
    payload = json.dumps({"cues": [{"id": "a", "text": "こんにちは"}]})
    assert validate_translation(payload, "STOP", ["a"]) == {"a": "こんにちは"}
    with pytest.raises(TranslationError):
        validate_translation(payload, "MAX_TOKENS", ["a"])


@pytest.mark.parametrize(
    "payload",
    [
        {"cues": []},
        {"cues": [{"id": "a", "text": "x"}, {"id": "a", "text": "x"}]},
        {"cues": [{"id": "a", "text": "x"}, {"id": "b", "text": "x"}]},
        {"cues": [{"id": "a", "text": " "}]},
    ],
)
def test_invalid_chunk_cannot_publish(payload):
    with pytest.raises(TranslationError):
        validate_translation(json.dumps(payload), "STOP", ["a"])


def test_truncated_json_cannot_publish():
    with pytest.raises(TranslationError):
        validate_translation('{"cues": [', "STOP", ["a"])


def setup_translation(tmp_path, count=1):
    from pydantic import SecretStr

    from video_content_capture.workspace.config import WorkspaceSettings
    from video_content_capture.workspace.storage import Library
    from video_content_capture.workspace.subtitles import Cue
    from video_content_capture.workspace.translation import TranslationResponse, TranslationService

    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 1000, "url", "{}")["id"])
    cues = [Cue(id=f"cue-{i}", start=i, end=i + 0.5, text="English") for i in range(count)]
    source = library.create_subtitle_version(video_id, "en", "original", "import", cues)
    settings = WorkspaceSettings(tmp_path, library.root, gemini_api_key=SecretStr("fake-sentinel"))

    class Fake:
        def __init__(self):
            self.calls = []
            self.fail = False
            self.after = None

        def translate(self, key, model, language, chunk):
            self.calls.append([cue.id for cue in chunk])
            if self.after:
                self.after()
            if self.fail and len(self.calls) == 2:
                return TranslationResponse("{}", "MAX_TOKENS")
            return TranslationResponse(
                json.dumps(
                    {"cues": [{"id": cue.id, "text": "日本語 fake-sentinel"} for cue in chunk]}
                ),
                "STOP",
            )

    fake = Fake()
    service = TranslationService(library, settings, fake)
    return library, video_id, source, fake, service


def run_translation(library, service, job):
    import threading

    attempt = library.start_job(str(job["id"]))
    service.run(job, attempt, threading.Event())
    return library.get_job(str(job["id"]))


def test_translation_copies_timing_redacts_and_reuses(tmp_path):
    library, video_id, source, fake, service = setup_translation(tmp_path)
    job = service.create(video_id, str(source["id"]), "ja")
    assert run_translation(library, service, job)["status"] == "completed"
    target_id = json.loads(str(job["snapshot"]))["target_version_id"]
    cue = library.subtitle_cues(target_id)[0]
    assert (cue.id, cue.start, cue.end, cue.text) == ("cue-0", 0, 0.5, "日本語 [REDACTED]")
    assert service.create(video_id, str(source["id"]), "ja")["id"] == job["id"]
    assert len(fake.calls) == 1
    assert service.create(video_id, str(source["id"]), "ja", regenerate=True)["id"] != job["id"]
    assert b"fake-sentinel" not in library.db_path.read_bytes()


def test_retry_resumes_only_completed_chunks(tmp_path):
    library, video_id, source, fake, service = setup_translation(tmp_path, 101)
    fake.fail = True
    job = service.create(video_id, str(source["id"]), "ja")
    assert run_translation(library, service, job)["status"] == "failed"
    target_id = json.loads(str(job["snapshot"]))["target_version_id"]
    assert not library.get_subtitle_version(target_id)["complete"]
    fake.fail = False
    library.retry_job(str(job["id"]))
    assert run_translation(library, service, job)["status"] == "completed"
    assert [len(call) for call in fake.calls] == [100, 1, 1]


def test_late_attempt_cannot_publish_after_cancel_retry(tmp_path):
    import threading

    library, video_id, source, fake, service = setup_translation(tmp_path)
    job = service.create(video_id, str(source["id"]), "ja")
    first = library.start_job(str(job["id"]))

    def replace_attempt():
        library.cancel_job(str(job["id"]))
        library.retry_job(str(job["id"]))
        library.start_job(str(job["id"]))

    fake.after = replace_attempt
    service.run(job, first, threading.Event())
    target_id = json.loads(str(job["snapshot"]))["target_version_id"]
    assert not library.get_subtitle_version(target_id)["complete"]
    assert library.stage_records(str(job["id"])) == []
    assert library.get_job(str(job["id"]))["status"] == "running"


def test_missing_key_has_zero_provider_calls(tmp_path):
    from video_content_capture.workspace.config import WorkspaceSettings

    library, video_id, source, fake, service = setup_translation(tmp_path)
    service.settings = WorkspaceSettings(tmp_path, library.root)
    with pytest.raises(TranslationError, match="GEMINI_API_KEY"):
        service.create(video_id, str(source["id"]), "ja")
    assert fake.calls == []
    assert library.list_jobs() == []


def test_sdk_client_explicit_key_no_retry_count_before_generation(monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue
    from video_content_capture.workspace.translation import GeminiTranslationAdapter

    observed = []

    class Models:
        def count_tokens(self, **kwargs):
            observed.append(("count", kwargs))
            return SimpleNamespace(total_tokens=50)

        def generate_content(self, **kwargs):
            observed.append(("generate", kwargs))
            return SimpleNamespace(
                candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value="STOP"))],
                prompt_feedback=None,
                text='{"cues":[{"id":"a","text":"你好"}]}',
            )

    class Client:
        def __init__(self, **kwargs):
            observed.append(("client", kwargs))
            self.models = Models()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("video_content_capture.workspace.translation.genai.Client", Client)
    response = GeminiTranslationAdapter().translate(
        SecretStr("fake-key"), "model-fixed", "zh-TW", [Cue(id="a", start=0, end=1, text="hello")]
    )
    assert response.finish_reason == "STOP"
    assert [entry[0] for entry in observed] == ["client", "count", "generate"]
    assert observed[0][1]["api_key"] == "fake-key"
    assert observed[0][1]["vertexai"] is False
    assert observed[0][1]["http_options"].retry_options.attempts == 1
    assert observed[1][1]["model"] == observed[2][1]["model"] == "model-fixed"
    assert observed[1][1]["contents"] == observed[2][1]["contents"]
    assert observed[2][1]["config"].response_mime_type == "application/json"


@pytest.mark.parametrize(
    "text,finish",
    [
        ('{"cues":[]}', "STOP"),
        ('{"cues":[{"id":"cue-0","text":"x"},{"id":"cue-0","text":"x"}]}', "STOP"),
        ('{"cues":[{"id":"extra","text":"x"}]}', "STOP"),
        ('{"cues":[{"id":"cue-0","text":""}]}', "STOP"),
        ('{"cues":[', "STOP"),
        ('{"cues":[{"id":"cue-0","text":"x"}]}', "MAX_TOKENS"),
        ('{"cues":[{"id":"cue-0","text":"x"}]}', "SAFETY"),
    ],
)
def test_invalid_response_leaves_version_unselectable(tmp_path, text, finish):
    from video_content_capture.workspace.translation import TranslationResponse

    library, video_id, source, fake, service = setup_translation(tmp_path)

    class InvalidAdapter:
        def translate(self, *args):
            return TranslationResponse(text, finish)

    service.adapter = InvalidAdapter()
    job = service.create(video_id, str(source["id"]), "ja")
    assert run_translation(library, service, job)["status"] == "failed"
    target_id = json.loads(str(job["snapshot"]))["target_version_id"]
    assert not library.get_subtitle_version(target_id)["complete"]
    assert library.subtitle_cues(target_id) == []
    with pytest.raises(ValueError):
        library.set_subtitle_selection(video_id, "playback", target_id)


def test_changed_chunk_identity_does_not_resume_stages(tmp_path):
    library, video_id, source, fake, service = setup_translation(tmp_path, 101)
    fake.fail = True
    job = service.create(video_id, str(source["id"]), "ja")
    assert run_translation(library, service, job)["status"] == "failed"
    fake.fail = False
    fresh = service.create(video_id, str(source["id"]), "ko")
    assert run_translation(library, service, fresh)["status"] == "completed"
    assert [len(call) for call in fake.calls] == [100, 1, 100, 1]


def test_provider_errors_are_actionable_and_preserve_retry_after(tmp_path):
    import httpx
    from google.genai.errors import ClientError

    library, video_id, source, fake, service = setup_translation(tmp_path)

    class RateLimited:
        def translate(self, *args):
            response = httpx.Response(429, headers={"Retry-After": "42"})
            raise ClientError(429, {"error": {"message": "fake-sentinel"}}, response)

    service.adapter = RateLimited()
    job = service.create(video_id, str(source["id"]), "ja")
    result = run_translation(library, service, job)
    assert result["error_code"] == "translation_rate_limited;retry_after=42s"
    assert b"fake-sentinel" not in library.db_path.read_bytes()


def test_changed_snapshot_fingerprint_fails_before_provider(tmp_path):
    library, video_id, source, fake, service = setup_translation(tmp_path)
    job = service.create(video_id, str(source["id"]), "ja")
    snapshot = json.loads(str(job["snapshot"]))
    snapshot["language"] = "ko"
    job["snapshot"] = json.dumps(snapshot)
    assert run_translation(library, service, job)["error_code"] == "translation_identity_changed"
    assert fake.calls == []


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "translation_key_invalid"),
        (403, "translation_permission_denied"),
        (404, "translation_model_missing"),
        (503, "translation_provider_unavailable"),
    ],
)
def test_provider_status_codes_never_preserve_error_body(status, code):
    from google.genai.errors import APIError

    from video_content_capture.workspace.translation import provider_error

    assert provider_error(APIError(status, {"error": {"message": "secret-body"}})) == code
