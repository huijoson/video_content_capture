"""Offline QA background workflow contracts and stale response fencing."""

import json
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.qa import QAResult, validate_answer
from video_content_capture.workspace.qa_jobs import QAService
from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue


def setup_qa(tmp_path: Path):
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 100, "url", "{}")["id"])
    source = library.create_subtitle_version(
        video_id,
        "en",
        "original",
        "platform_manual",
        [Cue(id="one", start=2.5, end=4, text="Original")],
    )
    conversation = library.create_conversation(video_id, str(source["id"]))
    settings = WorkspaceSettings(tmp_path, library.root, gemini_api_key=SecretStr("qa-sentinel"))

    class Fake:
        def __init__(self):
            self.calls = []
            self.after = None
            self.payload = json.dumps(
                {
                    "video_content": [{"claim": "Answer qa-sentinel", "cue_ids": ["one"]}],
                    "supplemental_knowledge": [],
                    "insufficient_evidence": [],
                }
            )
            self.finish = "STOP"

        def answer(self, key, model, cues, memory, question):
            self.calls.append((question, memory, cues))
            if self.after:
                self.after()
            return QAResult(
                validate_answer(self.payload, self.finish, cues), [m.id for m in memory]
            )

    fake = Fake()
    service = QAService(library, settings, fake)
    return library, video_id, conversation, fake, service


def run_message(library, service, message):
    job = library.get_job(str(message["request_id"]))
    attempt = library.start_job(str(job["id"]))
    service.run(job, attempt, threading.Event())
    return library.get_job(str(job["id"]))


def test_question_and_answer_redacted_before_storage(tmp_path):
    library, _, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "Question qa-sentinel")
    assert run_message(library, service, message)["status"] == "completed"
    assert fake.calls[0][0] == "Question [REDACTED]"
    messages = library.qa_messages(str(conversation["id"]))
    assert messages[0]["status"] == "completed"
    assert "qa-sentinel" not in str(messages)
    assert b"qa-sentinel" not in library.db_path.read_bytes()


def complete_turns(library, service, conversation_id, count):
    ids = []
    for index in range(count):
        message = service.create(conversation_id, f"q{index}")
        assert run_message(library, service, message)["status"] == "completed"
        ids.append(str(message["id"]))
    return ids


def test_memory_is_last_eight_successful_turns_excluding_failed_and_cancelled(tmp_path):
    library, _, conversation, fake, service = setup_qa(tmp_path)
    conversation_id = str(conversation["id"])
    completed = complete_turns(library, service, conversation_id, 10)
    fake.finish = "MAX_TOKENS"
    failed = service.create(conversation_id, "truncated")
    assert run_message(library, service, failed)["status"] == "failed"
    cancelled = service.create(conversation_id, "cancelled")
    library.cancel_job(str(cancelled["request_id"]))
    fake.finish = "STOP"
    current = service.create(conversation_id, "current")
    assert run_message(library, service, current)["status"] == "completed"
    sent = [turn.id for turn in fake.calls[-1][1]]
    assert sent == completed[-8:]
    assert fake.calls[-1][0] == "current"
    saved = library.get_qa_message(str(current["id"]))
    assert json.loads(str(saved["memory_ids"])) == completed[-8:]
    flags = {str(m["id"]): m["in_memory"] for m in library.qa_messages(conversation_id)}
    # The next request may reference the latest eight successful turns only.
    assert [flags[i] for i in completed[:3]] == [0, 0, 0]
    assert all(flags[i] == 1 for i in completed[3:] + [str(current["id"])])
    assert flags[str(failed["id"])] == flags[str(cancelled["id"])] == 0


@pytest.mark.parametrize(
    "payload,finish",
    [
        ("{", "STOP"),
        ("", "STOP"),
        (None, "MAX_TOKENS"),
        (None, "SAFETY"),
        (
            '{"video_content":[{"claim":"x","cue_ids":[]}],'
            '"supplemental_knowledge":[],"insufficient_evidence":[]}',
            "STOP",
        ),
        (
            '{"video_content":[{"claim":"x","cue_ids":["other-source"]}],'
            '"supplemental_knowledge":[],"insufficient_evidence":[]}',
            "STOP",
        ),
    ],
)
def test_invalid_provider_output_fails_without_persisted_answer_or_memory(
    tmp_path, payload, finish
):
    library, _, conversation, fake, service = setup_qa(tmp_path)
    if payload is not None:
        fake.payload = payload
    fake.finish = finish
    message = service.create(str(conversation["id"]), "question")
    job = run_message(library, service, message)
    assert job["status"] == "failed"
    assert str(job["error_code"]).startswith("qa_")
    saved = library.get_qa_message(str(message["id"]))
    assert saved["status"] == "failed"
    assert saved["response_json"] is None
    assert library.qa_memory(str(conversation["id"])) == []
    # Manual retry is allowed and reuses the same message with a new attempt.
    fake.payload, fake.finish = setup_payload(), "STOP"
    library.retry_job(str(job["id"]))
    assert run_message(library, service, message)["status"] == "completed"
    assert len(library.qa_messages(str(conversation["id"]))) == 1


def setup_payload():
    return json.dumps(
        {
            "video_content": [{"claim": "Answer", "cue_ids": ["one"]}],
            "supplemental_knowledge": [],
            "insufficient_evidence": [],
        }
    )


def start(library, message):
    job = library.get_job(str(message["request_id"]))
    return job, library.start_job(str(job["id"]))


def test_race_cancel_retry_then_old_attempt_reply_is_not_written(tmp_path):
    library, _, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, old = start(library, message)
    attempts = []

    def cancel_and_retry():
        library.cancel_job(str(job["id"]))
        library.retry_job(str(job["id"]))
        attempts.append(library.start_job(str(job["id"])))

    fake.after = cancel_and_retry
    # The old worker never observed its cancellation event: only the gate fences it.
    service.run(job, old, threading.Event())
    saved = library.get_qa_message(str(message["id"]))
    assert saved["status"] == "pending" and saved["response_json"] is None
    assert saved["attempt_id"] == attempts[0]
    fake.after = None
    service.run(library.get_job(str(job["id"])), attempts[0], threading.Event())
    assert library.get_qa_message(str(message["id"]))["status"] == "completed"
    assert library.get_job(str(job["id"]))["status"] == "completed"


def test_race_delete_conversation_then_new_conversation_old_reply_is_not_written(tmp_path):
    library, video_id, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, attempt = start(library, message)
    replacement = []

    def delete_and_recreate():
        library.delete_conversation(str(conversation["id"]))
        source = library.create_subtitle_version(
            video_id,
            "en",
            "reimported",
            "import",
            [Cue(id="one", start=2.5, end=4, text="Reimported")],
        )
        replacement.append(library.create_conversation(video_id, str(source["id"])))

    fake.after = delete_and_recreate
    service.run(job, attempt, threading.Event())
    new_id = str(replacement[0]["id"])
    assert library.qa_messages(new_id) == []
    assert library.get_video(video_id)["last_conversation_id"] == new_id
    with pytest.raises(ValueError):
        library.get_qa_message(str(message["id"]))
    assert library.get_job(str(job["id"]))["status"] == "cancelled"
    assert b"Answer" not in library.db_path.read_bytes()


def test_race_video_deleting_then_reimport_old_reply_is_not_written(tmp_path):
    library, video_id, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, attempt = start(library, message)
    reimport_errors = []

    def delete_video_and_reimport():
        library.mark_deleting(video_id)
        try:
            library.import_video("abcdefghijk", "影片", 100, "url", "{}")
        except ValueError as error:
            reimport_errors.append(error)

    fake.after = delete_video_and_reimport
    service.run(job, attempt, threading.Event())
    # A video marked deleting is never resurrected; a later new import has a new owner ID.
    assert reimport_errors
    assert library.get_qa_message(str(message["id"]))["status"] == "cancelled"
    assert library.get_qa_message(str(message["id"]))["response_json"] is None
    assert library.get_job(str(job["id"]))["status"] == "cancelled"


def test_race_source_switch_reply_stays_in_original_conversation(tmp_path):
    library, video_id, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, attempt = start(library, message)
    switched = []

    def switch_source():
        translation = library.create_subtitle_version(
            video_id,
            "zh-TW",
            "譯文",
            "translation",
            [Cue(id="one", start=2.5, end=4, text="譯文")],
            parent_id=str(conversation["source_version_id"]),
        )
        switched.append(library.create_conversation(video_id, str(translation["id"])))

    fake.after = switch_source
    service.run(job, attempt, threading.Event())
    new_id = str(switched[0]["id"])
    assert library.get_video(video_id)["last_conversation_id"] == new_id
    assert library.qa_messages(new_id) == []
    original = library.qa_messages(str(conversation["id"]))
    assert original[0]["status"] == "completed"
    answer = json.loads(str(original[0]["response_json"]))
    # Citations map against the original conversation's saved version.
    assert answer["video_content"][0]["citations"][0]["text"] == "Original"
    assert (
        library.get_conversation(str(conversation["id"]))["source_version_id"]
        == (conversation["source_version_id"])
    )


def test_race_switch_video_does_not_attach_reply_to_other_video(tmp_path):
    library, video_id, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, attempt = start(library, message)
    other = []

    def open_other_video():
        video = library.import_video("bbbbbbbbbbb", "其他", 100, "url", "{}")
        source = library.create_subtitle_version(
            str(video["id"]),
            "en",
            "other",
            "platform_manual",
            [Cue(id="one", start=2.5, end=4, text="Other")],
        )
        other.append(
            (str(video["id"]), library.create_conversation(str(video["id"]), str(source["id"])))
        )

    fake.after = open_other_video
    service.run(job, attempt, threading.Event())
    other_video, other_conversation = other[0]
    assert library.qa_messages(str(other_conversation["id"])) == []
    assert library.get_video(other_video)["last_conversation_id"] == other_conversation["id"]
    assert library.get_qa_message(str(message["id"]))["status"] == "completed"
    assert library.get_video(video_id)["last_conversation_id"] == conversation["id"]


def test_unpublished_stale_attempt_does_not_leave_job_running(tmp_path):
    library, _, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, attempt = start(library, message)

    def break_owner():
        # Simulates an owner change the gate rejects while this attempt is still current.
        with library._connect() as connection:
            connection.execute(
                "UPDATE qa_messages SET attempt_id='other' WHERE id=?", (message["id"],)
            )

    fake.after = break_owner
    service.run(job, attempt, threading.Event())
    assert library.get_job(str(job["id"]))["status"] == "failed"
    assert library.get_qa_message(str(message["id"]))["response_json"] is None


def test_missing_key_service_never_calls_adapter(tmp_path):
    library, _, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    service.settings = WorkspaceSettings(tmp_path, library.root)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        service.create(str(conversation["id"]), "another")
    assert run_message(library, service, message)["status"] == "failed"
    assert fake.calls == []


def test_race_purged_video_reimported_with_new_owner_never_receives_old_reply(tmp_path):
    library, video_id, conversation, fake, service = setup_qa(tmp_path)
    message = service.create(str(conversation["id"]), "question")
    job, attempt = start(library, message)
    reimported = []

    def delete_purge_and_reimport():
        library.mark_deleting(video_id)
        # Simulates the later cleanup step removing every row the deleting video owned.
        with library._connect() as connection:
            connection.execute(
                "DELETE FROM qa_messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE video_id=?)",
                (video_id,),
            )
            connection.execute("DELETE FROM conversations WHERE video_id=?", (video_id,))
            connection.execute(
                "DELETE FROM subtitle_cues WHERE version_id IN "
                "(SELECT id FROM subtitle_versions WHERE video_id=?)",
                (video_id,),
            )
            connection.execute("DELETE FROM subtitle_versions WHERE video_id=?", (video_id,))
            connection.execute("DELETE FROM jobs WHERE video_id=?", (video_id,))
            connection.execute("DELETE FROM videos WHERE id=?", (video_id,))
        video = library.import_video("abcdefghijk", "影片", 100, "url", "{}")
        source = library.create_subtitle_version(
            str(video["id"]),
            "en",
            "original",
            "platform_manual",
            [Cue(id="one", start=2.5, end=4, text="Original")],
        )
        new_conversation = library.create_conversation(str(video["id"]), str(source["id"]))
        pending = library.create_qa_message(str(new_conversation["id"]), "new", "model")
        reimported.append((str(video["id"]), new_conversation, pending))

    fake.after = delete_purge_and_reimport
    service.run(job, attempt, threading.Event())
    new_video, new_conversation, pending = reimported[0]
    assert new_video != video_id
    messages = library.qa_messages(str(new_conversation["id"]))
    assert [m["id"] for m in messages] == [pending["id"]]
    assert messages[0]["status"] == "pending" and messages[0]["response_json"] is None
    assert b"Answer" not in library.db_path.read_bytes()


def test_qa_runs_while_media_job_is_running(tmp_path):
    library, _, conversation, _, service = setup_qa(tmp_path)
    video_id = str(conversation["video_id"])
    media = library.create_job(video_id, "v", "a")
    library.start_job(str(media["id"]))
    message = service.create(str(conversation["id"]), "question")
    # Spec section 3: QA is available once source text is ready while media work continues.
    job, attempt = start(library, message)
    assert attempt
    other = service.create(
        str(library.create_conversation(video_id, conversation["source_version_id"])["id"]), "q2"
    )
    with pytest.raises(ValueError):
        start(library, other)


def test_provider_failure_logs_redacted_warning_without_question(tmp_path, caplog):
    import logging

    from google.genai.errors import ClientError

    library, video_id, conversation, fake, service = setup_qa(tmp_path)

    class Rejected:
        def answer(self, *args):
            raise ClientError(400, {"error": {"message": "API key not valid qa-sentinel"}})

    service.adapter = Rejected()
    message = service.create(str(conversation["id"]), "private question text")
    with caplog.at_level(logging.WARNING, logger="vcc.workspace"):
        job = run_message(library, service, message)
    assert job["error_code"] == "qa_key_invalid"
    records = [r for r in caplog.records if r.name == "vcc.workspace"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    line = records[0].getMessage()
    for part in (str(job["id"]), "stage=qa", "code=400", "[REDACTED]"):
        assert part in line
    assert "qa-sentinel" not in line
    assert "private question text" not in line
