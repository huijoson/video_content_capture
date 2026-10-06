# One-Click Flow Retry, Cancel and Restart Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a one-click flow be retried from its failed stage without re-downloading or re-translating, cancelled along with its running child process, and recognized as needing a manual retry after a service restart.

**Architecture:** Add two storage primitives (`Library.reopen_flow`, `Library.latest_flow`), two flow-coordinator methods (`FlowService.cancel`, `FlowService.retry`) that reuse the existing per-stage job retry and the existing `MediaQueue.cancel` cascade, two HTTP routes, one new confirm-screen field (`latest_flow_id`), and a small action block in the flow panel. No new job kind, no new state machine: `FlowService.advance()` stays the only place a flow changes stage.

**Tech Stack:** Python 3.13, FastAPI, sqlite3 (raw SQL via `Library`), Pydantic v2, vanilla JavaScript in `static/workspace.js`, pytest with `fastapi.testclient.TestClient`.

**Spec:** `docs/superpowers/specs/2026-10-07-one-click-flow-retry-design.md`

## Global Constraints

- Python 3.13 only (`requires-python = ">=3.13,<3.14"`); no new runtime dependency.
- Every user-facing string is Traditional Chinese (`zh-TW`) and reuses the wording already in `workspace.js`/`app.py` where one exists; **never** introduce Simplified Chinese (this repo has already had one such regression on `main`).
- `static/workspace.js` must not use `innerHTML`; build nodes with `document.createElement` and set `textContent` (asserted by `tests/test_workspace_ui.py`).
- All state changes stay inside `Library`; services call library methods rather than issuing SQL.
- Writes that can race a worker use `BEGIN IMMEDIATE`.
- Quality gates after every task: `ruff check .`, `ruff format --check .`, `mypy`, `pytest`.
- The flow status payload shape (`{flow, snapshot, stages, artifact}`) is a contract with `workspace.js`; only `confirm()` gains a key, and only `latest_flow_id`.
- `FlowService` never imports `MediaQueue`; collaborators are injected through `bind`/`bind_cancel`.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/video_content_capture/workspace/storage.py` | All SQLite state; owns the `flows` table invariants | Modify: add `reopen_flow`, `latest_flow` next to the existing flow methods |
| `src/video_content_capture/workspace/flows.py` | Flow coordinator: the stage state machine and the frozen snapshot | Modify: add `bind_cancel`, `cancel`, `retry`; add one `confirm()` key |
| `src/video_content_capture/workspace/app.py` | HTTP surface and wiring | Modify: add two routes, one binding, one confirm passthrough |
| `src/video_content_capture/workspace/static/index.html` | Flow panel markup | Modify: add the `flow-actions` button row |
| `src/video_content_capture/workspace/static/workspace.js` | Flow panel behaviour | Modify: button labelling, handlers, `latest_flow_id` adoption, poll economy |
| `tests/test_workspace_storage.py` | Storage-level flow invariants | Modify: extend with `reopen_flow`/`latest_flow` cases |
| `tests/test_workspace_flow_api.py` | Flow HTTP behaviour end to end | Modify: add retry, cancel, restart-recovery and 409-contract tests |
| `tests/test_workspace_ui.py` | Static UI contract | Modify: assert the new controls and phrases |

No file is created; every change lands in an existing module with an established pattern.

---

### Task 1: Storage primitives for reviving and locating a flow

**Files:**
- Modify: `src/video_content_capture/workspace/storage.py` (insert after `active_flow`, ~line 1382)
- Test: `tests/test_workspace_storage.py` (extend after `test_flow_lifecycle_is_frozen_singular_and_interrupted_on_restart`, line 279)

**Interfaces:**
- Consumes: `Library.create_flow`, `Library.update_flow`, `Library.interrupt_running`, `Library.get_video`, `Library._connect`, `Library._record` — all existing.
- Produces:
  - `Library.reopen_flow(self, flow_id: str) -> Record` — revives `failed|cancelled|interrupted` to `running`, returns a `running` flow unchanged, raises `ValueError` for `completed` and for an unknown id, raises `ValueError("Flow already in progress")` when another flow for the same video is running.
  - `Library.latest_flow(self, video_id: str) -> Record | None` — newest flow for the video in any status.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workspace_storage.py`:

```python
def test_reopen_flow_revives_only_ended_flows_and_keeps_one_running(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    flow = library.create_flow(video_id, "subtitles", {"target_language": "en"})
    flow_id = str(flow["id"])

    # Running is idempotent: the coordinator may re-enter retry on a live flow.
    assert str(library.reopen_flow(flow_id)["id"]) == flow_id
    assert str(library.reopen_flow(flow_id)["status"]) == "running"

    library.update_flow(flow_id, status="failed", error="media_failed")
    revived = library.reopen_flow(flow_id)
    assert revived["status"] == "running"
    assert revived["error_code"] is None
    assert str(library.active_flow(video_id)["id"]) == flow_id  # type: ignore[index]

    library.update_flow(flow_id, status="cancelled", error="cancelled")
    assert str(library.reopen_flow(flow_id)["status"]) == "running"
    library.interrupt_running()
    assert str(library.reopen_flow(flow_id)["status"]) == "running"

    # A completed flow is finished for good; retrying it would re-publish an artifact.
    library.update_flow(flow_id, status="completed")
    with pytest.raises(ValueError, match="finished"):
        library.reopen_flow(flow_id)

    with pytest.raises(ValueError):
        library.reopen_flow("0123456789abcdef")


def test_reopen_flow_refuses_while_another_flow_is_running(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    ended = library.create_flow(video_id, "download", {"target_language": "en"})
    library.update_flow(str(ended["id"]), status="failed", error="media_failed")
    running = library.create_flow(video_id, "download", {"target_language": "en"})

    with pytest.raises(ValueError, match="already in progress"):
        library.reopen_flow(str(ended["id"]))
    assert library.get_flow(str(ended["id"]))["status"] == "failed"
    assert str(library.active_flow(video_id)["id"]) == running["id"]  # type: ignore[index]


def test_latest_flow_returns_the_newest_flow_in_any_status(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    assert library.latest_flow(video_id) is None

    first = library.create_flow(video_id, "download", {"target_language": "en"})
    library.update_flow(str(first["id"]), status="failed", error="media_failed")
    second = library.create_flow(video_id, "download", {"target_language": "en"})
    assert str(library.latest_flow(video_id)["id"]) == second["id"]  # type: ignore[index]

    library.update_flow(str(second["id"]), status="cancelled")
    latest = library.latest_flow(video_id)
    # A finished flow still leads, which is what the reloaded page needs to label it.
    assert latest is not None and latest["status"] == "cancelled"

    with pytest.raises(ValueError):
        library.latest_flow("0123456789abcdef")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_workspace_storage.py -k "reopen_flow or latest_flow" -v`
Expected: FAIL with `AttributeError: 'Library' object has no attribute 'reopen_flow'` and `... 'latest_flow'`.

- [ ] **Step 3: Implement the storage methods**

Insert into `src/video_content_capture/workspace/storage.py` directly after `active_flow` (after line 1382, before `def flow_jobs`):

```python
    def latest_flow(self, video_id: str) -> Record | None:
        """The newest flow in any status; the reloaded page uses it to label its last attempt."""
        self.get_video(video_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT * FROM flows WHERE video_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (video_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip((column[0] for column in cursor.description), row, strict=True))

    def reopen_flow(self, flow_id: str) -> Record:
        """Revive an ended flow so `advance` can chain its next stage again.

        `update_flow` only ever writes `status='running'` rows, so a failed, cancelled or
        interrupted flow needs this explicit transition. A completed flow is finished for
        good: retrying it would re-publish an artifact the user already has.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            flow = self._record(
                connection.execute("SELECT * FROM flows WHERE id = ?", (flow_id,))
            )
            if flow["status"] == "running":
                return flow
            if flow["status"] == "completed":
                raise ValueError("Flow already finished")
            if connection.execute(
                "SELECT 1 FROM flows WHERE video_id = ? AND status = 'running' AND id != ?",
                (flow["video_id"], flow_id),
            ).fetchone():
                # Same wording as `create_flow`: the UI treats both as one busy flow.
                raise ValueError("Flow already in progress")
            connection.execute(
                "UPDATE flows SET status = 'running', error_code = NULL, updated_at = ? "
                "WHERE id = ?",
                (datetime.now(UTC).isoformat(), flow_id),
            )
            return self._record(connection.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_workspace_storage.py -v`
Expected: PASS (all tests in the file, including the pre-existing flow lifecycle test).

- [ ] **Step 5: Run the four gates**

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```
Expected: all clean; `pytest` reports 695 passed (692 baseline + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/video_content_capture/workspace/storage.py tests/test_workspace_storage.py
git commit -m "feat(storage): revive an ended flow and locate a video's latest flow (#7)"
```

---

### Task 2: Flow cancel and retry, coordinator and routes

**Files:**
- Modify: `src/video_content_capture/workspace/flows.py` (`__init__`/`bind` at lines 120-133; new methods after `on_job_finished`, ~line 273; `confirm` return dict, ~line 211)
- Modify: `src/video_content_capture/workspace/app.py` (wiring at line 169; the two routes after `flow_status`, line 644)
- Test: `tests/test_workspace_flow_api.py`

**Interfaces:**
- Consumes: `Library.reopen_flow`, `Library.latest_flow` (Task 1); `Library.retry_job`, `Library.flow_jobs`, `Library.get_job`, `Library.get_flow`, `Library.update_flow`, `FlowService.status`, `FlowService.advance`, `FlowService.enqueue`.
- Produces:
  - `FlowService.bind_cancel(self, cancel: Callable[[str], None]) -> None`
  - `FlowService.cancel(self, flow_id: str) -> Record` — returns `status(flow_id)`
  - `FlowService.retry(self, flow_id: str) -> Record` — returns `status(flow_id)`
  - `FlowService.confirm(...)` payload gains `"latest_flow_id": str | None`
  - `POST /api/flows/{flow_id}/cancel` and `POST /api/flows/{flow_id}/retry`, both returning the `status()` payload on 200
  - Module constant `FLOW_CANCEL_MESSAGE = "這條流程已經結束"`, `FLOW_RUNNING_MESSAGE = "這條流程仍在進行"`, `FLOW_DONE_MESSAGE = "這條流程已經完成"`, `FLOW_NO_STAGE_MESSAGE = "這條流程沒有可重試的階段工作，請重新開始"`, `FLOW_MKV_MESSAGE = "請改選 MKV，重新確認摘要並建立新的匯出工作"`, `FLOW_UNBOUND_MESSAGE = "取消功能尚未啟用"`

The service and its two routes land together: the tests below drive the HTTP surface, and spec §5.5's
"every rejection is checked before the first write" is only observable through it. Splitting them would
leave a task whose tests cannot pass on their own.

- [ ] **Step 1: Write the failing API tests**

First extend the harness in `tests/test_workspace_flow_api.py` — the retry tests need a way to fail a *later* stage and to count translation calls.

Replace `FakeTranslation` with a counting version:

```python
class FakeTranslation:
    def __init__(self) -> None:
        # Counted so a retry can prove it reused a finished translation instead of redoing it.
        self.calls = 0

    def translate(
        self, key: SecretStr, model: str, language: str, cues: list[Cue]
    ) -> TranslationResponse:
        self.calls += 1
        return TranslationResponse(
            json.dumps({"cues": [{"id": cue.id, "text": "翻譯"} for cue in cues]}), "STOP"
        )
```

Add a one-shot subtitle failure to `FlowAdapter`:

```python
class FlowAdapter(FakeAdapter):
    """FakeAdapter plus the original language and exactly one platform subtitle track."""

    def __init__(self, language: str = "en", automatic: bool = False) -> None:
        super().__init__()
        self.source = parse_metadata(
            {
                "id": "abcdefghijk",
                "title": "離線影片",
                "duration": 10,
                "language": language,
                "automatic_captions" if automatic else "subtitles": {language: [{"ext": "vtt"}]},
                "formats": [
                    {"format_id": "v", "height": 720, "vcodec": "avc1", "acodec": "none"},
                    {"format_id": "a", "vcodec": "none", "acodec": "mp4a"},
                ],
            },
            "https://youtu.be/abcdefghijk",
        )
        self.subtitles_downloaded = 0
        # Set to an exception to fail the subtitle stage; cleared to let a retry succeed.
        self.subtitle_failure: Exception | None = None

    def download_subtitle(self, source: SourceMetadata, track, cancel: Event) -> bytes:
        self.subtitles_downloaded += 1
        if self.subtitle_failure is not None:
            raise self.subtitle_failure
        return VTT
```

Then append the two tests:

```python
def make_flow_client(
    tmp_path: Path,
    adapter: FlowAdapter,
    runner: FakeFFmpeg,
    translation: FakeTranslation | None = None,
):
    """A client with a Gemini key so a translating flow can be driven end to end."""
    library = Library(tmp_path / "library")
    library.initialize()
    settings = replace(
        load_settings(tmp_path, library_dir=library.root),
        gemini_api_key=SecretStr("fake-api-sentinel"),
    )
    client = TestClient(
        create_app(
            settings,
            adapter,
            translation_adapter=translation or FakeTranslation(),
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(runner),
        ),
        base_url="http://127.0.0.1:8765",
    )
    return client, library


def _flow_id(library: Library, flow_id: str) -> str:
    """The stored id, typed: `Record` values are `object` under mypy strict."""
    return str(library.get_flow(flow_id)["id"])


def test_rejected_retry_and_cancel_leave_the_flow_untouched(tmp_path: Path) -> None:
    """Cancelling a queued stage is enough; retrying a live flow must change nothing."""
    adapter = HoldingAdapter()
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        # The fake adapter answers the same metadata for any URL, so the second video has
        # to be imported directly; that is the pattern the existing cancel test uses.
        other = library.import_video(
            "lmnopqrstuv",
            "第二部",
            10,
            "https://youtu.be/lmnopqrstuv",
            adapter.source.model_copy(update={"youtube_id": "lmnopqrstuv"}).model_dump_json(),
        )
        blocking = client.post(
            f"/api/videos/{other['id']}/jobs",
            json={"format_id": "v", "audio_id": "a"},
            headers=ORIGIN,
        ).json()
        assert adapter.entered.wait(5)
        try:
            started = start_flow(client, video["id"], "en")
            assert started.status_code == 200, started.text
            created = _flow_id(library, started.json()["flow"]["id"])
            stage_id = str(started.json()["job"]["id"])
            assert stage_id != blocking["id"]
            assert library.get_job(stage_id)["status"] == "queued"

            # Retrying a live flow is a client error, and the row is left alone.
            retried = client.post(f"/api/flows/{created}/retry", headers=ORIGIN)
            assert retried.status_code == 409, retried.text
            assert library.get_flow(created)["status"] == "running"

            unknown = client.post("/api/flows/0123456789abcdef/cancel", headers=ORIGIN)
            assert unknown.status_code == 404

            cancelled = client.post(f"/api/flows/{created}/cancel", headers=ORIGIN)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["flow"]["status"] == "cancelled"
            assert cancelled.json()["flow"]["error_code"] == "cancelled"
            assert library.get_job(stage_id)["status"] == "cancelled"

            # Cancelling is final; the flow may still be resumed from its first stage.
            assert client.post(f"/api/flows/{created}/cancel", headers=ORIGIN).status_code == 409
            resumed = client.post(f"/api/flows/{created}/retry", headers=ORIGIN)
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["flow"]["status"] == "running"
        finally:
            adapter.release.set()


def test_retry_resumes_the_failed_stage_without_downloading_again(tmp_path: Path) -> None:
    """A subtitle stage that fails must not restart the download it already published."""
    adapter = FlowAdapter("en")
    adapter.subtitle_failure = SourceError("subtitle_unavailable", "字幕已消失")
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "zh-TW")
        assert started.status_code == 200, started.text
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        status = wait_for_flow(client, flow_id, expect="failed")
        assert status["flow"]["stage"] == "subtitles"
        assert status["flow"]["error_code"] == "subtitle_unavailable"
        assert [stage["status"] for stage in status["stages"]] == [
            "completed",
            "failed",
            "pending",
            "pending",
        ]
        assert status["stages"][1]["error_code"] == "subtitle_unavailable"
        assert adapter.downloads == 1

        adapter.subtitle_failure = None
        resumed = client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["flow"]["id"] == flow_id
        finished = wait_for_flow(client, flow_id)
        assert {stage["status"] for stage in finished["stages"]} == {"completed"}
        # The retry resumed at the subtitle stage; the published download was reused.
        assert adapter.downloads == 1
        listed = confirm(client, video["id"], target_language="zh-TW").json()
        assert listed["latest_flow_id"] == flow_id


def test_retry_after_a_chaining_failure_redoes_only_the_chain(tmp_path: Path) -> None:
    """A stage that succeeded while the next job could not be built is retryable as is.

    `FakeFFmpeg(hardware=False)` refuses the VideoToolbox probe, and the burned export probes
    it while *building* the export job — inside `FlowService._export_job`, which runs inside
    `advance()`. So the translation stage completes and the flow dies in chaining with
    `error_code == "flow_failed"`, `stage` still `translation` and no export job at all. That
    is exactly the `completed`-stage branch of spec §5.2 step 6.
    """
    runner = FakeFFmpeg()
    runner.hardware = False
    translation = FakeTranslation()
    adapter = FlowAdapter("en")
    client, library = make_flow_client(tmp_path, adapter, runner, translation)
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "zh-TW")
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        status = wait_for_flow(client, flow_id, expect="failed")
        assert status["flow"]["stage"] == "translation"
        assert status["flow"]["error_code"] == "flow_failed"
        assert translation.calls == 1
        translated = next(
            job for job in library.flow_jobs(flow_id) if job["kind"] == "translation"
        )
        translated_id, attempt = str(translated["id"]), str(translated["attempt_id"])
        assert translated["status"] == "completed"
        assert [job["kind"] for job in library.flow_jobs(flow_id)] == ["download", "subtitles"]
        published = [asset["id"] for asset in library.assets(video["id"])]
        assert published

        runner.hardware = True
        resumed = client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN)
        assert resumed.status_code == 200, resumed.text
        finished = wait_for_flow(client, flow_id)
        assert finished["artifact"] is not None
        # Not one earlier stage ran again: same counts, same assets, same translation attempt.
        assert adapter.downloads == 1
        assert adapter.subtitles_downloaded == 1
        assert translation.calls == 1
        assert [asset["id"] for asset in library.assets(video["id"])] == published
        again = library.get_job(translated_id)
        assert again["attempt_id"] == attempt
        assert again["status"] == "completed"
        assert [job["kind"] for job in library.flow_jobs(flow_id)] == [
            "download",
            "subtitles",
            "translation",
            "export",
        ]
```

An export-stage failure that resumes mid-export is already covered end to end by the existing
`tests/test_workspace_exports.py::test_insufficient_space_fails_the_job_before_writing_anything`; this plan
does not duplicate it, and a flow-level variant would need its own disk-space fake whose probe the
chaining path never reaches.

The last two tests in this step pin the routes' own contract:

```python
def test_flow_routes_report_unknown_flows_and_refuse_a_finished_one(tmp_path: Path) -> None:
    adapter = FlowAdapter("en")
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        assert client.post("/api/flows/0123456789abcdef/retry", headers=ORIGIN).status_code == 404
        started = start_flow(client, video["id"], "en")
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        wait_for_flow(client, flow_id)
        assert client.post(f"/api/flows/{flow_id}/cancel", headers=ORIGIN).status_code == 409
        assert client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN).status_code == 409


def test_restart_marks_the_flow_interrupted_and_lets_the_ui_retry_it(tmp_path: Path) -> None:
    """A restarted service must label its unfinished flow instead of forgetting it."""
    adapter = BlockingAdapter()
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "en")
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        assert adapter.entered.wait(5)

        # What `Library.initialize()` does on the next service start, while the worker runs.
        library.interrupt_running()
        adapter.release.set()
        assert library.get_flow(flow_id)["status"] == "interrupted"
        listed = confirm(client, video["id"], target_language="en").json()
        assert listed["latest_flow_id"] == flow_id
        assert listed["busy"] is False
        assert client.get(f"/api/flows/{flow_id}").json()["flow"]["status"] == "interrupted"

        resumed = client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN)
        assert resumed.status_code == 200, resumed.text
        finished = wait_for_flow(client, flow_id)
        assert {stage["status"] for stage in finished["stages"]} == {"completed"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_workspace_flow_api.py -k "rejected_retry or retry_resumes or chaining_failure or flow_routes_report or restart_marks" -v`
Expected: FAIL — before Step 7 nothing serves `/api/flows/{id}/cancel` or `/api/flows/{id}/retry` (405/404),
and `wait_for_flow(..., expect=...)` is not a parameter yet.

- [ ] **Step 3: Teach `wait_for_flow` about ended flows, and import the failure type**

In `tests/test_workspace_flow_api.py`, replace the existing `wait_for_flow`:

```python
def wait_for_flow(client: TestClient, flow_id: str, expect: str = "completed") -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        payload = client.get(f"/api/flows/{flow_id}").json()
        if payload["flow"]["status"] != "running":
            assert payload["flow"]["status"] == expect, payload
            return payload
        time.sleep(0.01)
    pytest.fail("One-click flow did not finish within 10 seconds")
```

Then widen the import of the metadata module at the top of the file:

```python
from video_content_capture.workspace.youtube import SourceError, SourceMetadata, parse_metadata
```

- [ ] **Step 4: Add the coordinator constants and binding**

In `src/video_content_capture/workspace/flows.py`, after `TARGET_MESSAGE` (line 39) add:

```python
FLOW_CANCEL_MESSAGE = "這條流程已經結束"
FLOW_RUNNING_MESSAGE = "這條流程仍在進行"
FLOW_DONE_MESSAGE = "這條流程已經完成"
FLOW_NO_STAGE_MESSAGE = "這條流程沒有可重試的階段工作，請重新開始"
FLOW_MKV_MESSAGE = "請改選 MKV，重新確認摘要並建立新的匯出工作"
FLOW_UNBOUND_MESSAGE = "取消功能尚未啟用"
```

Change `FlowService.__init__` and `flow`'s `bind` block:

```python
    def __init__(
        self,
        library: Library,
        settings: WorkspaceSettings,
        acquisition: AcquisitionService,
        translation: TranslationService,
        exporter: ExportService,
    ) -> None:
        self.library, self.settings = library, settings
        self.acquisition, self.translation, self.exporter = acquisition, translation, exporter
        self.enqueue: Callable[[Record], None] = lambda job: None
        self.cancel_job: Callable[[str], None] | None = None

    def bind(self, enqueue: Callable[[Record], None]) -> None:
        """The coordinator runs inside the worker's completion hook, so enqueue is injected."""
        self.enqueue = enqueue

    def bind_cancel(self, cancel: Callable[[str], None]) -> None:
        """Cancelling a stage must terminate its child process, which only the queue can do."""
        self.cancel_job = cancel
```

- [ ] **Step 5: Implement `cancel` and `retry`**

In `src/video_content_capture/workspace/flows.py`, insert after `on_job_finished` (before `advance`):

```python
    def _stage_job(self, flow_id: str, stage: str) -> Record | None:
        """The job that carries the flow's current stage, if it was ever created."""
        for job in self.library.flow_jobs(flow_id):
            if str(job["kind"]) == stage:
                return job
        return None

    def cancel(self, flow_id: str) -> Record:
        """End a running flow and terminate the child process of its current stage.

        Cancelling the stage job is enough: `MediaQueue.cancel` signals a running attempt
        and finishes a queued one itself, and `advance` turns that into a cancelled flow.
        """
        flow = self.library.get_flow(flow_id)
        if flow["status"] != "running":
            raise FlowError(FLOW_CANCEL_MESSAGE)
        job = self._stage_job(flow_id, str(flow["stage"]))
        if job is not None and str(job["status"]) in {"queued", "running"}:
            if self.cancel_job is None:
                # Without the queue hook a "cancelled" flow could leave ffmpeg running.
                raise FlowError(FLOW_UNBOUND_MESSAGE)
            self.cancel_job(str(job["id"]))
        else:
            # Nothing queued or running: the stage never produced work to cancel.
            self.library.update_flow(flow_id, status="cancelled", error="cancelled")
        return self.status(flow_id)

    def retry(self, flow_id: str) -> Record:
        """Resume an ended flow from its failed stage, reusing every finished artifact."""
        flow = self.library.get_flow(flow_id)
        status = str(flow["status"])
        if status == "running":
            raise FlowError(FLOW_RUNNING_MESSAGE)
        if status == "completed":
            raise FlowError(FLOW_DONE_MESSAGE)
        job = self._stage_job(flow_id, str(flow["stage"]))
        if job is None:
            # Reopening without work would leave a running flow blocking every later start.
            raise FlowError(FLOW_NO_STAGE_MESSAGE)
        if str(job["kind"]) == "translation" and not self.settings.gemini_api_key:
            raise FlowKeyError(MISSING_KEY_MESSAGE)
        if str(job["error_code"]) == "container_confirmation_required":
            raise FlowError(FLOW_MKV_MESSAGE)
        # Reopen first: a fast worker must not finish the job before the flow accepts it.
        self.library.reopen_flow(flow_id)
        if str(job["status"]) == "completed":
            # The stage succeeded and chaining failed; redo the chaining, not the stage.
            return self.status(str(self.advance(flow_id, job)["id"]))
        self.library.retry_job(str(job["id"]))
        self.enqueue(self.library.get_job(str(job["id"])))
        return self.status(flow_id)
```

- [ ] **Step 6: Expose `latest_flow_id` on the confirm payload**

In `confirm`'s return dict, next to `"busy"` (line 211), add:

```python
            "latest_flow_id": (
                str(latest["id"]) if (latest := self.library.latest_flow(video_id)) else None
            ),
```

- [ ] **Step 7: Wire the cancel hook and add the two routes**

In `src/video_content_capture/workspace/app.py`, extend the wiring block (lines 168-169):

```python
    flows.bind(queue.enqueue)
    flows.bind_cancel(queue.cancel)
    queue.bind(flows.on_job_finished)
```

`FlowCancel`/`FlowRetry` response models are not needed: both routes return `flows.status(flow_id)`, a
plain `Record`, exactly as `flow_status` does. The `flows` import at the top of the file already brings in
`FlowError` and `FlowKeyError`, so no import changes.

Add after the `flow_status` route (after line 644):

```python
    @app.post("/api/flows/{flow_id}/cancel")
    def cancel_flow(flow_id: str) -> Record:
        """Stop the whole flow, including the child process of its current stage."""
        try:
            return flows.cancel(flow_id)
        except FlowError as error:
            raise HTTPException(409, str(error)) from None
        except (KeyError, ValueError):
            raise HTTPException(404, "流程不存在") from None

    @app.post("/api/flows/{flow_id}/retry")
    def retry_flow(flow_id: str) -> Record:
        """Resume from the failed stage; finished downloads and translations are reused."""
        try:
            return flows.retry(flow_id)
        except FlowError as error:
            raise HTTPException(409, str(error)) from None
        except (KeyError, ValueError):
            raise HTTPException(404, "流程不存在") from None
```

One `except FlowError` covers `FlowKeyError` too, since it is a subclass, and `str(error)` already carries
the existing wording — the same shape the job-level `retry` route uses.

- [ ] **Step 8: Run the flow API tests**

Run: `pytest tests/test_workspace_flow_api.py -v`
Expected: PASS for the new tests and all pre-existing ones.

- [ ] **Step 9: Run the four gates**

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```

Expected: all clean; `pytest` reports 699 passed (692 baseline + 3 storage + 4 flow API).

- [ ] **Step 10: Commit**

```bash
git add src/video_content_capture/workspace/flows.py src/video_content_capture/workspace/app.py tests/test_workspace_flow_api.py
git commit -m "feat(flow): cancel and retry a one-click flow from its failed stage (#7)"
```

---

### Task 3: Flow panel controls and restart visibility

**Files:**
- Modify: `src/video_content_capture/workspace/static/index.html` (flow panel, line ~110)
- Modify: `src/video_content_capture/workspace/static/workspace.js` (`renderFlowStatus` line ~451, `refreshFlow` line ~480, `requestFlowConfirm` line ~390)
- Test: `tests/test_workspace_ui.py`

**Interfaces:**
- Consumes: `POST /api/flows/{id}/cancel`, `POST /api/flows/{id}/retry`, `latest_flow_id` from `GET /api/videos/{id}/flow`.
- Produces: element ids `flow-actions`, `cancel-flow`, `retry-flow`; the phrases 「取消流程」「重試流程」「已中斷，需手動重試」.

- [ ] **Step 1: Write the failing UI contract test**

Append to `tests/test_workspace_ui.py`:

```python
def test_one_click_flow_exposes_cancel_and_retry_controls() -> None:
    page = Elements()
    page.feed((STATIC / "index.html").read_text())
    for element_id in ("flow-actions", "cancel-flow", "retry-flow"):
        assert element_id in page.by_id
    assert page.by_id["cancel-flow"]["tag"] == "button"
    assert page.by_id["retry-flow"]["tag"] == "button"
    # Both controls start hidden; the panel reveals only the one that applies.
    assert "hidden" in page.by_id["flow-actions"]
    script = (STATIC / "workspace.js").read_text()
    assert "取消流程" in script and "重試流程" in script
    assert "需手動重試" in script
    assert '"/cancel"' in script and '"/retry"' in script
    assert "/api/flows/${controlled(flowId)}" in script
    # A reloaded page adopts the video's latest flow so an interrupted one is visible.
    assert "latest_flow_id" in script
    assert "innerHTML" not in script
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_workspace_ui.py -k cancel_and_retry -v`
Expected: FAIL with `assert 'flow-actions' in page.by_id`.

- [ ] **Step 3: Add the markup**

In `src/video_content_capture/workspace/static/index.html`, between the `flow-stages` list and `<p id="flow-artifact"></p>`:

```html
          <div id="flow-actions" class="button-row" hidden>
            <button id="cancel-flow" type="button">取消流程</button>
            <button id="retry-flow" type="button">重試流程</button>
          </div>
```

- [ ] **Step 4: Adopt the latest flow when the page has none**

In `src/video_content_capture/workspace/static/workspace.js`, change `refreshFlow`:

```javascript
async function refreshFlow() {
  if (!currentVideo) { flowConfirm = null; flowConfirmVideoId = null; flowStatus = null; flowId = null; renderFlow(); return; }
  flowConfirming = true;
  try {
    await requestFlowConfirm();
    // A reload keeps no flow id; adopt the video's latest so an interrupted flow is visible.
    if (!flowId && flowConfirm?.latest_flow_id) flowId = flowConfirm.latest_flow_id;
    if (flowId && (!flowStatus || ["queued", "running"].includes(flowStatus.flow.status))) {
      flowStatus = await request(`/api/flows/${controlled(flowId)}`);
    }
  } catch (error) {
    flowConfirm = null; flowConfirmVideoId = null; flowStatus = null;
    element("flow-summary").textContent = "確認內容暫時無法讀取，請稍後再試。";
    throw error;
  } finally {
    flowConfirming = false;
  }
  renderFlow();
}
```

- [ ] **Step 5: Render the controls**

In `renderFlowStatus`, replace the artifact block with the artifact plus controls, and add the interrupted wording to the summary:

```javascript
function renderFlowStatus() {
  const stages = element("flow-stages");
  stages.replaceChildren();
  const payload = flowStatus;
  let summary = !payload
    ? "尚未開始一鍵流程。"
    : `流程 ${statusLabels[payload.flow.status] || payload.flow.status} · ${payload.stages.filter((stage) => stage.status === "completed").length}/${payload.stages.length} 階段完成`;
  if (payload?.flow.status === "interrupted") summary += " · 已中斷，需手動重試";
  else if (payload?.flow.status === "failed" && payload.flow.error_code) summary += ` · ${jobErrorLabel(payload.flow.error_code)}`;
  element("flow-status").textContent = summary;
  if (payload) {
    for (const stage of payload.stages) {
      const item = document.createElement("li");
      const label = flowStepLabels[stage.stage] || stage.stage;
      const percent = stage.progress == null ? "" : ` · ${Math.round(stage.progress * 100)}%`;
      item.textContent = `${statusLabels[stage.status] ? statusIcons[stage.status] : statusIcons.info} ${label}：${statusLabels[stage.status] || stage.status}${percent}${stage.error_code ? ` · ${jobErrorLabel(stage.error_code)}` : ""}`;
      stages.append(item);
    }
  }
  renderFlowActions();
  const artifact = element("flow-artifact");
  artifact.replaceChildren();
  if (payload?.artifact) {
    const link = document.createElement("a");
    link.href = `/api/exports/${controlled(payload.artifact.id)}/download`;
    link.textContent = "下載影片";
    link.setAttribute("download", "");
    const label = document.createElement("span");
    label.textContent = `已完成：${payload.artifact.summary ? "成品就緒" : ""} `;
    artifact.append(label, link);
  }
}

function renderFlowActions() {
  const actions = element("flow-actions");
  const status = flowStatus?.flow.status ?? null;
  actions.hidden = !status;
  if (!status) return;
  // A confirmation-required export can only be redone by choosing MKV, so no dead button.
  const retryable = ["failed", "cancelled", "interrupted"].includes(status) &&
    flowStatus.flow.error_code !== "container_confirmation_required";
  element("cancel-flow").hidden = status !== "running";
  element("retry-flow").hidden = !retryable;
}

async function flowAction(button, action) {
  if (!flowId || button.disabled) return;
  button.disabled = true;
  try {
    flowStatus = await request(`/api/flows/${controlled(flowId)}${action}`, "POST");
    renderFlow();
    await pollJobs();
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
}

element("cancel-flow").addEventListener("click", () => flowAction(element("cancel-flow"), "/cancel"));
element("retry-flow").addEventListener("click", () => flowAction(element("retry-flow"), "/retry"));
```

- [ ] **Step 6: Run the UI tests**

Run: `pytest tests/test_workspace_ui.py tests/test_workspace_s5_ui.py tests/test_workspace_qa_ui.py -v`
Expected: PASS.

- [ ] **Step 7: Run the four gates**

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```

- [ ] **Step 8: Commit**

```bash
git add src/video_content_capture/workspace/static/index.html src/video_content_capture/workspace/static/workspace.js tests/test_workspace_ui.py
git commit -m "feat(ui): cancel, retry and label a one-click flow from the panel (#7)"
```

---

### Task 4: Prove a cancelled flow stops its ffmpeg child, and close the ticket

**Files:**
- Test: `tests/test_workspace_flow_api.py`
- Modify: `docs/design/youtube-workspace-v1.md` only if it states a behaviour this work contradicts (it should not — read it first and change nothing when it already matches).

**Interfaces:**
- Consumes: everything from Tasks 1-3; `FakeFFmpeg` with its `block`/`entered`/`release`/`cancelled` events.
- Produces: the final ticket evidence.

- [ ] **Step 1: Write the test**

It is written before the fix only in the sense that matters here: nobody has yet shown that the
flow-level cancel reaches ffmpeg. `MediaQueue.cancel` already signals a running attempt and
`advance()` already turns the resulting `cancelled` job into a `cancelled` flow, so this step should
pass on the first run; if it does not, the defect is in the `bind_cancel` wiring from Task 2 Step 7
and has to be fixed there before continuing.

```python
def test_cancelling_a_flow_stops_its_running_ffmpeg_child(tmp_path: Path) -> None:
    """The flow-level cancel must reach the running export, not only its database row."""
    runner = FakeFFmpeg()
    adapter = FlowAdapter("en")
    client, library = make_flow_client(tmp_path, adapter, runner)
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        # `FakeFFmpeg.block` only arms the real burn: the export chaining probe always runs
        # ffmpeg with `-t 0.1`, and that path never blocks.
        runner.block = True
        started = start_flow(client, video["id"], "en")
        assert started.status_code == 200, started.text
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        assert runner.entered.wait(5), "the export stage never reached ffmpeg"

        cancelled = client.post(f"/api/flows/{flow_id}/cancel", headers=ORIGIN)
        assert cancelled.status_code == 200, cancelled.text
        assert runner.cancelled.wait(5), "the flow cancel never reached the ffmpeg child"
        status = wait_for_flow(client, flow_id, expect="cancelled")
        assert status["flow"]["error_code"] == "cancelled"
        assert status["stages"][-1]["status"] == "cancelled"
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_workspace_flow_api.py -k stops_its_running_ffmpeg -v`
Expected: PASS. A failure means the flow-level cancel does not reach a live export; fix Task 2's
wiring rather than weakening this test.

- [ ] **Step 3: Run the whole suite and the other gates**

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```
Expected: all clean, 699 tests passing (692 baseline + 3 storage + 4 flow API). Nothing else should move;
if the UI test from Task 3 fails here, Task 3 regressed and has to be fixed in its own commit.

- [ ] **Step 4: Commit**

```bash
git add tests/test_workspace_flow_api.py
git commit -m "test(flow): prove cancelling a flow stops its ffmpeg child (#7)"
```

- [ ] **Step 5: Post the ticket evidence and close #7**

```bash
gh issue comment 7 --body "$(git log --format='- %h %s' 9da321a..HEAD)"
gh issue close 7
```

Include in the comment the six acceptance criteria and the test that covers each, plus the four gate results, following `docs/agents/issue-tracker.md`.

---

## Self-Review

**1. Spec coverage**

| Spec section | Task | Test that proves it |
| --- | --- | --- |
| §5.1 `reopen_flow`, `latest_flow` | Task 1 | 3 new tests in `tests/test_workspace_storage.py` |
| §5.2 `bind_cancel`, `cancel`, `retry`, `confirm` key | Task 2 Steps 4-7 | `rejected_retry...`, `retry_resumes...`, `chaining_failure...`, `restart_marks...` |
| §5.3 HTTP routes and error codes | Task 2 Step 7 | `flow_routes_report...`, `rejected_retry...` |
| §5.4 UI markup, labels, handlers, adoption | Task 3 | `test_one_click_flow_exposes_cancel_and_retry_controls` |
| §5.5 error ordering, `BUSY_MESSAGE`, cascade rationale | Task 2 Steps 5-8, Task 4 | `rejected_retry...` (nothing written on rejection) |
| §6.1 failed stage and reason | Task 2 Step 1 | `retry_resumes_the_failed_stage_without_downloading_again` |
| §6.2 no re-download or re-translation | Task 2 Step 1 | the same test plus `retry_after_a_chaining_failure_redoes_only_the_chain` |
| §6.3 cancel terminates the child | Task 4 | `test_cancelling_a_flow_stops_its_running_ffmpeg_child` |
| §6.4 duplicate start 409 | Task 2 Step 7, plus the pre-existing test kept | `test_duplicate_start_is_conflicted_while_the_flow_runs`, `flow_routes_report...` |
| §6.5 restart labelling and recovery | Task 2 Step 1 | `test_restart_marks_the_flow_interrupted_and_lets_the_ui_retry_it` |
| §6.6 storage and service contracts | Tasks 1, 2 | the storage tests plus the four flow API tests |
| §6.7 UI contract | Task 3 Step 1 | `test_one_click_flow_exposes_cancel_and_retry_controls` |
| §6.8 four gates | Every task | `ruff check . && ruff format --check . && mypy && pytest -q` |

No spec section is left without a task, and no test lives in a task whose code it needs.

**2. Placeholder scan**

Every step carries the code to type, the command to run and the result to expect. There is no
"implement this however you like" step and no step that asks the implementer to invent a name,
a message or a test body.

**3. Type consistency**

- `reopen_flow(flow_id: str) -> Record` and `latest_flow(video_id: str) -> Record | None` are used with those exact names and shapes in Tasks 2 and 3.
- `FlowService.cancel(flow_id) -> Record` and `FlowService.retry(flow_id) -> Record` are named the same in the service, the routes and the tests.
- The binding name is `bind_cancel` in the service, `cancel_job` as the attribute, and `queue.cancel` as the wired callable — consistent with the spec's `flows.bind_cancel(queue.cancel)`.
- The JSON key is `latest_flow_id` in `confirm()`, the routes, the tests and the JS.
- Element ids `flow-actions`, `cancel-flow`, `retry-flow` match across markup, script and tests.
- `MediaQueue.cancel(job_id)` is called as an instance method through the injected callable, so the service never needs a `Record`.
- Every new test reads ids through `_flow_id(library, ...)`, which returns `str`; `Record` values are `object` under mypy strict, so a bare `started.json()["flow"]["id"]` would not type-check.

**4. Sequencing**

Tasks 1 → 2 → 3 are strictly ordered (each consumes the previous one's interface). Task 4 consumes
Tasks 1-3 and closes the ticket, so it is last. Every task ends green on its own: no task contains a
test whose route, hook or element another task has not added yet.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-07-one-click-flow-retry.md`.

The plan is deliberately self-contained: a fresh subagent per task can start from this file alone, and each task ends with the four gates plus its own commit, so a reviewer can reject any single task without blocking its neighbours.
