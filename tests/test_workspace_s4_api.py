"""S4 conversation and QA HTTP contracts with fake providers only."""

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.test_workspace_s2_api import FakeAdapter
from tests.test_workspace_s3_api import ORIGIN, SRT
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.qa import QAResult, validate_answer
from video_content_capture.workspace.storage import Library

SECRET = "api-sentinel-value"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VCC_LIBRARY_DIR"):
        monkeypatch.delenv(name, raising=False)


class FakeQA:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def answer(self, key, model, cues, memory, question):
        self.calls.append((question, memory, cues))
        self.entered.set()
        assert self.release.wait(5)
        return QAResult(
            validate_answer(
                json.dumps(
                    {
                        "video_content": [{"claim": f"Answer {SECRET}", "cue_ids": [cues[0].id]}],
                        "supplemental_knowledge": ["Background"],
                        "insufficient_evidence": [],
                    }
                ),
                "STOP",
                cues,
            ),
            [m.id for m in memory],
        )


def import_source(client, video_id, name="Original"):
    response = client.post(
        f"/api/videos/{video_id}/subtitles/import?language=en&name={name}&format=srt",
        content=SRT,
        headers=ORIGIN,
    )
    assert response.status_code == 200
    return response.json()["version"]["id"]


def query_video(client, youtube_id="abcdefghijk"):
    return client.post(
        "/api/query",
        json={"url": f"https://youtu.be/{youtube_id}"},
        headers=ORIGIN,
    ).json()["id"]


def test_missing_key_and_no_source_make_zero_qa_calls(tmp_path: Path):
    fake = FakeQA()
    settings = WorkspaceSettings(tmp_path, tmp_path / "library")
    with TestClient(
        create_app(settings, FakeAdapter(), qa_adapter=fake), base_url="http://127.0.0.1:8765"
    ) as client:
        video_id = query_video(client)
        base = f"/api/videos/{video_id}/conversations"
        assert client.get(base).json()["conversations"] == []
        assert client.post(base, json={"version_id": None}, headers=ORIGIN).status_code == 400
        source_id = import_source(client, video_id)
        conversation = client.post(base, json={"version_id": source_id}, headers=ORIGIN).json()
        response = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": "Question"},
            headers=ORIGIN,
        )
        assert response.status_code == 409
        assert "GEMINI_API_KEY" in response.text
        assert fake.calls == []
        assert client.get("/api/jobs").json() == []


def keyed_client(tmp_path: Path, fake: FakeQA) -> TestClient:
    settings = WorkspaceSettings(tmp_path, tmp_path / "library", gemini_api_key=SecretStr(SECRET))
    return TestClient(
        create_app(settings, FakeAdapter(), qa_adapter=fake), base_url="http://127.0.0.1:8765"
    )


def wait_for_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("QA job did not finish within 5 seconds")


def test_ask_flow_concurrency_citations_and_secret_never_persisted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    fake = FakeQA()
    with keyed_client(tmp_path, fake) as client:
        video_id = query_video(client)
        source_id = import_source(client, video_id)
        base = f"/api/videos/{video_id}/conversations"
        conversation = client.post(base, json={"version_id": source_id}, headers=ORIGIN).json()
        url = f"/api/conversations/{conversation['id']}/messages"
        fake.release.clear()
        first = client.post(url, json={"question": f"Q {SECRET}"}, headers=ORIGIN)
        assert first.status_code == 200
        assert fake.entered.wait(5)
        # Same conversation: one request at a time.
        assert client.post(url, json={"question": "again"}, headers=ORIGIN).status_code == 409
        fake.release.set()
        job = wait_for_job(client, first.json()["request_id"])
        assert job["status"] == "completed"
        assert fake.calls[0][0] == "Q [REDACTED]"
        second = client.post(url, json={"question": "follow-up"}, headers=ORIGIN)
        assert second.status_code == 200
        wait_for_job(client, second.json()["request_id"])
        assert [turn.id for turn in fake.calls[1][1]] == [first.json()["id"]]
        shown = client.get(f"/api/conversations/{conversation['id']}")
        messages = shown.json()["messages"]
        assert [m["status"] for m in messages] == ["completed", "completed"]
        citation = messages[0]["response"]["video_content"][0]["citations"][0]
        cues = client.get(f"/api/subtitles/{source_id}/cues").json()
        assert citation == {
            "cue_id": cues[0]["id"],
            "start": cues[0]["start"],
            "end": cues[0]["end"],
            "text": cues[0]["text"],
        }
        assert messages[1]["memory_ids"] == [messages[0]["id"]]
        listing = client.get(base).json()
        assert listing["current_conversation_id"] == conversation["id"]
        assert SECRET not in shown.text
        assert SECRET not in client.get("/api/jobs").text
    assert SECRET not in caplog.text
    for artifact in (tmp_path / "library").rglob("*"):
        if artifact.is_file():
            assert SECRET.encode() not in artifact.read_bytes()


def test_new_source_creates_conversation_and_old_one_is_restorable(tmp_path: Path):
    fake = FakeQA()
    with keyed_client(tmp_path, fake) as client:
        video_id = query_video(client)
        first_source = import_source(client, video_id, "First")
        second_source = import_source(client, video_id, "Second")
        base = f"/api/videos/{video_id}/conversations"
        first = client.post(base, json={"version_id": first_source}, headers=ORIGIN).json()
        asked = client.post(
            f"/api/conversations/{first['id']}/messages", json={"question": "Q"}, headers=ORIGIN
        ).json()
        wait_for_job(client, asked["request_id"])
        second = client.post(base, json={"version_id": second_source}, headers=ORIGIN).json()
        assert second["id"] != first["id"]
        assert second["source_version_id"] == second_source and second["messages"] == []
        assert client.get(f"/api/videos/{video_id}").json()["last_conversation_id"] == second["id"]
        old = client.get(f"/api/conversations/{first['id']}").json()
        assert old["source_version_id"] == first_source
        assert old["messages"][0]["status"] == "completed"
        selected = client.post(f"{base}/{first['id']}/select", headers=ORIGIN)
        assert selected.status_code == 200
        reopened = client.get(f"/api/videos/{video_id}").json()
        assert reopened["last_conversation_id"] == first["id"]
        assert reopened["qa_version_id"] == first_source
        assert client.delete(f"/api/conversations/{first['id']}", headers=ORIGIN).json() == {
            "deleted": True
        }
        assert client.get(f"/api/conversations/{first['id']}").status_code == 404
        assert [c["id"] for c in client.get(base).json()["conversations"]] == [second["id"]]


def test_restart_marks_pending_failed_and_never_resends(tmp_path: Path):
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("abcdefghijk", "影片", 100, "https://youtu.be/abcdefghijk", "{}")
    from video_content_capture.workspace.subtitles import Cue

    source = library.create_subtitle_version(
        str(video["id"]), "en", "o", "platform_manual", [Cue(id="a", start=0, end=1, text="x")]
    )
    conversation = library.create_conversation(str(video["id"]), str(source["id"]))
    message = library.create_qa_message(str(conversation["id"]), "question", "model")
    fake = FakeQA()
    with keyed_client(tmp_path, fake) as client:
        time.sleep(0.1)
        job = client.get(f"/api/jobs/{message['request_id']}").json()
        assert job["status"] == "interrupted"
        shown = client.get(f"/api/conversations/{conversation['id']}").json()
        assert shown["messages"][0]["status"] == "failed"
        assert shown["messages"][0]["error_code"] == "interrupted"
        assert fake.calls == []
        retried = client.post(f"/api/jobs/{message['request_id']}/retry", headers=ORIGIN)
        assert retried.status_code == 200
        assert wait_for_job(client, message["request_id"])["status"] == "completed"
        assert len(fake.calls) == 1


def ask_question(client, video_id, question="Q"):
    source_id = import_source(client, video_id)
    base = f"/api/videos/{video_id}/conversations"
    conversation = client.post(base, json={"version_id": source_id}, headers=ORIGIN).json()
    url = f"/api/conversations/{conversation['id']}/messages"
    response = client.post(url, json={"question": question}, headers=ORIGIN)
    assert response.status_code == 200
    return conversation, response.json()


def test_api_cancel_then_retry_ignores_old_attempt_and_answers_once(tmp_path: Path):
    fake = FakeQA()
    with keyed_client(tmp_path, fake) as client:
        video_id = query_video(client)
        fake.release.clear()
        conversation, message = ask_question(client, video_id)
        job_id = message["request_id"]
        assert fake.entered.wait(5)
        cancelled = client.post(f"/api/jobs/{job_id}/cancel", headers=ORIGIN)
        assert cancelled.json()["status"] == "cancelled"
        shown = client.get(f"/api/conversations/{conversation['id']}").json()
        assert shown["messages"][0]["status"] == "cancelled"
        assert client.post(f"/api/jobs/{job_id}/retry", headers=ORIGIN).status_code == 200
        # A second question cannot start while the retried request is pending.
        again = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": "again"},
            headers=ORIGIN,
        )
        assert again.status_code == 409
        fake.release.set()
        assert wait_for_job(client, job_id)["status"] == "completed"
        messages = client.get(f"/api/conversations/{conversation['id']}").json()["messages"]
        assert len(messages) == 1
        assert messages[0]["status"] == "completed"
        assert len(messages[0]["response"]["video_content"]) == 1
        assert len(fake.calls) == 2


def test_api_token_limit_failure_is_shown_retryable_and_excluded_from_memory(tmp_path: Path):
    from video_content_capture.workspace.qa import QAError

    class LimitedQA(FakeQA):
        def answer(self, key, model, cues, memory, question):
            if question == "too long":
                self.calls.append((question, memory, cues))
                raise QAError("qa_token_limit")
            return super().answer(key, model, cues, memory, question)

    fake = LimitedQA()
    with keyed_client(tmp_path, fake) as client:
        video_id = query_video(client)
        conversation, message = ask_question(client, video_id, "too long")
        job = wait_for_job(client, message["request_id"])
        assert job["status"] == "failed" and job["error_code"] == "qa_token_limit"
        shown = client.get(f"/api/conversations/{conversation['id']}").json()["messages"]
        assert shown[0]["status"] == "failed"
        assert shown[0]["error_code"] == "qa_token_limit"
        assert shown[0]["response"] is None and shown[0]["in_memory"] == 0
        follow = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": "short"},
            headers=ORIGIN,
        ).json()
        wait_for_job(client, follow["request_id"])
        assert fake.calls[-1][1] == []
        shown = client.get(f"/api/conversations/{conversation['id']}").json()["messages"]
        assert shown[1]["memory_ids"] == []
        retried = client.post(f"/api/jobs/{message['request_id']}/retry", headers=ORIGIN)
        assert retried.status_code == 200
        assert wait_for_job(client, message["request_id"])["error_code"] == "qa_token_limit"
        assert [call[0] for call in fake.calls] == ["too long", "short", "too long"]


def test_conversation_routes_reject_cross_video_access(tmp_path: Path):
    fake = FakeQA()
    with keyed_client(tmp_path, fake) as client:
        first_video = query_video(client)
        second_video = query_video(client, "bbbbbbbbbbb")
        first_source = import_source(client, first_video)
        conversation = client.post(
            f"/api/videos/{first_video}/conversations",
            json={"version_id": first_source},
            headers=ORIGIN,
        ).json()
        # A version or conversation from another video can never be attached.
        assert (
            client.post(
                f"/api/videos/{second_video}/conversations",
                json={"version_id": first_source},
                headers=ORIGIN,
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/videos/{second_video}/conversations/{conversation['id']}/select",
                headers=ORIGIN,
            ).status_code
            == 404
        )
        assert client.get(f"/api/videos/{second_video}").json()["last_conversation_id"] is None
        assert fake.calls == []


def test_qa_answers_while_download_is_still_running(tmp_path: Path):
    fake = FakeQA()
    adapter = FakeAdapter()
    entered, release = threading.Event(), threading.Event()

    def blocking():
        entered.set()
        assert release.wait(5)

    adapter.callback = blocking
    settings = WorkspaceSettings(tmp_path, tmp_path / "library", gemini_api_key=SecretStr(SECRET))
    with TestClient(
        create_app(settings, adapter, qa_adapter=fake), base_url="http://127.0.0.1:8765"
    ) as client:
        video_id = query_video(client)
        source_id = import_source(client, video_id)
        download = client.post(
            f"/api/videos/{video_id}/jobs", json={"format_id": "v", "audio_id": "a"}, headers=ORIGIN
        ).json()
        assert entered.wait(5)
        base = f"/api/videos/{video_id}/conversations"
        conversation = client.post(base, json={"version_id": source_id}, headers=ORIGIN).json()
        sent = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": "Q"},
            headers=ORIGIN,
        ).json()
        try:
            assert wait_for_job(client, sent["request_id"])["status"] == "completed"
            assert client.get(f"/api/jobs/{download['id']}").json()["status"] == "running"
        finally:
            release.set()
