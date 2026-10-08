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


def batched_adapter(monkeypatch, respond):
    """Monkeypatch the SDK and record one entry per generate_content call."""
    import re
    from types import SimpleNamespace

    from video_content_capture.workspace.translation import GeminiTranslationAdapter

    calls = []

    class Models:
        def count_tokens(self, **kwargs):
            return SimpleNamespace(total_tokens=10)

        def generate_content(self, **kwargs):
            ids = re.findall(r'"id": "([^"]+)"', kwargs["contents"])
            calls.append(ids)
            return SimpleNamespace(
                candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value="STOP"))],
                prompt_feedback=None,
                text=respond(ids),
            )

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("video_content_capture.workspace.translation.genai.Client", Client)
    return GeminiTranslationAdapter(), calls


def test_adapter_translates_in_bounded_batches_and_merges_every_id(monkeypatch):
    import json as json_module

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue

    adapter, calls = batched_adapter(
        monkeypatch,
        lambda ids: json_module.dumps({"cues": [{"id": i, "text": i} for i in ids]}),
    )
    cues = [Cue(id=f"c{i:03d}", start=i, end=i + 1, text="word") for i in range(45)]
    response = adapter.translate(SecretStr("fake-key"), "model-fixed", "zh-TW", cues)
    # A single oversized request makes the model merge neighbouring fragments.
    assert [len(batch) for batch in calls] == [20, 20, 5]
    assert response.finish_reason == "STOP"
    merged = json_module.loads(response.text)
    assert [cue["id"] for cue in merged["cues"]] == [cue.id for cue in cues]
    assert all(cue["text"] == cue["id"] for cue in merged["cues"])


def test_adapter_fails_closed_when_a_batch_blanks_a_merged_cue(monkeypatch):
    import json as json_module

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue

    def respond(ids):
        # Mimics the model folding a fragment into its neighbour.
        return json_module.dumps(
            {"cues": [{"id": i, "text": "" if i == "c021" else i} for i in ids]}
        )

    adapter, calls = batched_adapter(monkeypatch, respond)
    cues = [Cue(id=f"c{i:03d}", start=i, end=i + 1, text="word") for i in range(45)]
    with pytest.raises(TranslationError) as error:
        adapter.translate(SecretStr("fake-key"), "model-fixed", "zh-TW", cues)
    assert "translation_empty" in str(error.value)
    # Fails on the offending batch, so the later one is never requested.
    assert [len(batch) for batch in calls] == [20, 20]


def test_adapter_fails_closed_when_a_batch_drops_a_cue(monkeypatch):
    import json as json_module

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue

    def respond(ids):
        # The absorbed cue never comes back at all.
        return json_module.dumps({"cues": [{"id": i, "text": i} for i in ids if i != "c021"]})

    adapter, _ = batched_adapter(monkeypatch, respond)
    cues = [Cue(id=f"c{i:03d}", start=i, end=i + 1, text="word") for i in range(45)]
    with pytest.raises(TranslationError) as error:
        adapter.translate(SecretStr("fake-key"), "model-fixed", "zh-TW", cues)
    assert "translation_ids" in str(error.value)


def test_adapter_merges_reordered_batches_back_into_source_order(monkeypatch):
    import json as json_module

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue

    def respond(ids):
        return json_module.dumps({"cues": [{"id": i, "text": i} for i in reversed(ids)]})

    adapter, _ = batched_adapter(monkeypatch, respond)
    cues = [Cue(id=f"c{i:03d}", start=i, end=i + 1, text="word") for i in range(25)]
    response = adapter.translate(SecretStr("fake-key"), "model-fixed", "zh-TW", cues)
    merged = json_module.loads(response.text)
    assert [cue["id"] for cue in merged["cues"]] == [cue.id for cue in cues]


def test_adapter_sends_batches_in_source_order(monkeypatch):
    import json as json_module

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue

    adapter, calls = batched_adapter(
        monkeypatch,
        lambda ids: json_module.dumps({"cues": [{"id": i, "text": i} for i in ids]}),
    )
    cues = [Cue(id=f"c{i:03d}", start=i, end=i + 1, text="word") for i in range(41)]
    adapter.translate(SecretStr("fake-key"), "model-fixed", "zh-TW", cues)
    assert calls[0][0] == "c000" and calls[1][0] == "c020" and calls[2][0] == "c040"


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


def capture_generate_config(monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from video_content_capture.workspace.subtitles import Cue
    from video_content_capture.workspace.translation import GeminiTranslationAdapter

    observed = {}

    class Models:
        def count_tokens(self, **kwargs):
            return SimpleNamespace(total_tokens=50)

        def generate_content(self, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value="STOP"))],
                prompt_feedback=None,
                text='{"cues":[{"id":"a","text":"你好"}]}',
            )

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("video_content_capture.workspace.translation.genai.Client", Client)
    GeminiTranslationAdapter().translate(
        SecretStr("fake-key"), "model-fixed", "zh-TW", [Cue(id="a", start=0, end=1, text="hello")]
    )
    return observed["config"]


def test_generation_uses_json_schema_not_response_schema(monkeypatch):
    from video_content_capture.workspace.translation import TranslatedChunk

    config = capture_generate_config(monkeypatch)
    assert config.response_schema is None
    assert config.response_json_schema == TranslatedChunk.model_json_schema()


def test_developer_api_config_has_no_additional_properties(monkeypatch):
    from types import SimpleNamespace

    from google.genai import models

    config = capture_generate_config(monkeypatch)
    assert "additional_properties" not in config.model_dump(exclude_none=True)
    # Same converter the SDK applies when vertexai=False.
    wire = models._GenerateContentConfig_to_mldev(SimpleNamespace(vertexai=False), config, {})
    assert "responseSchema" not in wire
    assert wire["responseJsonSchema"] == config.response_json_schema


@pytest.mark.parametrize(
    "body",
    [
        {
            "error": {
                "code": 400,
                "message": "secret-body",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                    }
                ],
            }
        },
        {
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
            }
        },
    ],
)
def test_gemini_invalid_key_400_maps_to_key_invalid(body):
    from google.genai.errors import APIError

    from video_content_capture.workspace.translation import provider_error

    assert provider_error(APIError(400, body)) == "translation_key_invalid"


def test_plain_400_stays_request_failed():
    from google.genai.errors import APIError

    from video_content_capture.workspace.translation import provider_error

    error = APIError(400, {"error": {"message": "Bad field", "status": "INVALID_ARGUMENT"}})
    assert provider_error(error) == "translation_provider_request_failed"


def test_provider_failure_logs_one_redacted_warning(tmp_path, caplog):
    import logging

    from google.genai.errors import ClientError

    library, video_id, source, fake, service = setup_translation(tmp_path)

    class Rejected:
        def translate(self, *args):
            raise ClientError(400, {"error": {"message": "bad key fake-sentinel", "status": "X"}})

    service.adapter = Rejected()
    job = service.create(video_id, str(source["id"]), "ja")
    with caplog.at_level(logging.WARNING, logger="vcc.workspace"):
        assert run_translation(library, service, job)["status"] == "failed"
    records = [r for r in caplog.records if r.name == "vcc.workspace"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    line = records[0].getMessage()
    for part in (str(job["id"]), "stage=translation", "code=400", "status=X", "[REDACTED]"):
        assert part in line
    assert "fake-sentinel" not in line
    assert "English" not in line
