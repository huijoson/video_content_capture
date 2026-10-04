"""Snapshot-bound QA jobs, published through the shared attempt gate."""

import json
import threading

from google.genai import errors

from video_content_capture.redaction import scrub_text
from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.qa import (
    GeminiQAAdapter,
    MemoryTurn,
    QAAdapter,
    QAError,
    provider_error,
)
from video_content_capture.workspace.storage import Library, Record


class QAService:
    def __init__(
        self,
        library: Library,
        settings: WorkspaceSettings,
        adapter: QAAdapter | None = None,
    ) -> None:
        self.library, self.settings = library, settings
        self.adapter = adapter or GeminiQAAdapter()

    def create(self, conversation_id: str, question: str) -> Record:
        key = self.settings.gemini_api_key
        if not key:
            raise ValueError("請在專案 .env 設定 GEMINI_API_KEY 並重啟服務")
        question = scrub_text(question, [key.get_secret_value()]).strip()
        if not question or len(question) > 20_000:
            raise ValueError("請輸入最多 20,000 字的問題")
        model = self.settings.qa_model
        if scrub_text(model, [key.get_secret_value()]) != model:
            raise ValueError("問答模型設定無效")
        return self.library.create_qa_message(conversation_id, question, model)

    def run(self, job: Record, attempt: str, cancel: threading.Event) -> None:
        job_id = str(job["id"])
        try:
            key = self.settings.gemini_api_key
            if not key:
                raise ValueError("missing_gemini_key")
            snapshot = json.loads(str(job["snapshot"]))
            conversation_id = str(snapshot["conversation_id"])
            source_id = str(snapshot["source_version_id"])
            conversation = self.library.get_conversation(conversation_id)
            source = self.library.get_subtitle_version(source_id)
            message = next(
                message
                for message in self.library.qa_messages(conversation_id)
                if message["id"] == snapshot["message_id"]
            )
            if (
                conversation["video_id"] != job["video_id"]
                or conversation["source_version_id"] != source_id
                or source["video_id"] != job["video_id"]
                or not source["complete"]
                or message["status"] != "pending"
                or message["attempt_id"] != attempt
            ):
                raise ValueError("qa_owner_changed")
            memory = [
                MemoryTurn(
                    str(turn["id"]), str(turn["question"]), json.loads(str(turn["response_json"]))
                )
                for turn in self.library.qa_memory(conversation_id)
            ]
            if cancel.is_set():
                return
            result = self.adapter.answer(
                key,
                str(message["model"]),
                self.library.subtitle_cues(source_id),
                memory,
                str(message["question"]),
            )
            if cancel.is_set():
                return
            # Redaction is repeated at the persistence boundary, including injected adapters.
            answer: Record = json.loads(
                scrub_text(json.dumps(result.answer, ensure_ascii=False), [key.get_secret_value()])
            )
            if not self.library.publish_qa_message(job_id, attempt, answer, result.memory_ids):
                # Fenced: stale attempt or changed owner. Never write the late answer.
                self.library.update_attempt(job_id, attempt, "failed", error="qa_owner_changed")
        except errors.APIError as error:
            code = provider_error(error)
            self.library.update_attempt(job_id, attempt, "failed", error=code)
        except QAError as error:
            key = self.settings.gemini_api_key
            code = scrub_text(str(error), [key.get_secret_value()] if key else [])
            self.library.update_attempt(job_id, attempt, "failed", error=code)
        except Exception:
            self.library.update_attempt(job_id, attempt, "failed", error="qa_failed")
