# One-Click Flow Retry, Cancel and Restart Design

- **Date:** 2026-10-07
- **Status:** Approved design
- **Scope:** `video_content_capture.workspace` (storage, flow coordination, HTTP API, browser UI) and its tests
- **Issue:** [#7 一鍵流程的失敗、取消與重試](https://github.com/huijoson/video_content_capture/issues/7) (epic #1)
- **Governing policy:** `docs/design/youtube-workspace-v1.md` §7

## 1. Goal

Close the remaining acceptance criteria of issue #7: a one-click flow must be recoverable after it fails, cancellable while it runs, and legible after a service restart.

The work must:

1. Show the failed stage and its reason, while keeping the already published media and subtitle versions.
2. Retry from the failed stage without downloading again and without translating again.
3. Cancel the whole flow and terminate the media child process it owns.
4. Keep answering a repeated start with `409` while a flow runs.
5. After a service restart, mark the unfinished flow as needing a manual retry — in the UI, not only in the database.
6. Cover each of the above with HTTP API tests.

## 2. Non-goals

- Automatic retry. Design §7 fixes that a restart marks running work `interrupted` and never re-sends an API request on its own.
- A new job kind, a new queue, or a second flow record for the same attempt.
- Issue #13 (`VVT`/`VTT` parsing) and any translation-chunking change.
- Changing how a *started* flow's snapshot freezes the user's choices.
- Reworking the per-job cancel/retry buttons, which already exist and stay as they are.

## 3. Current behaviour (verified)

- A flow is one `flows` row plus one `jobs` row per stage carrying `flow_id`. `flows_one_running` is a partial unique index on `flows(video_id) WHERE status='running'`.
- `Library.update_flow()` only touches rows `WHERE status='running'`, so a `failed`, `cancelled` or `interrupted` flow can only be revived by a new storage method.
- `FlowService.advance()` returns early unless the flow is `running` and its `stage` equals the finished job's `kind`; a `cancelled` job cancels the flow, any other non-`completed` status fails it, and `completed` chains the next stage.
- `FlowService.on_job_finished()` fails the flow with `error_code='flow_failed'` when chaining itself raises — so a flow can be ended by a *stage that succeeded*.
- `MediaQueue.cancel()` sets the job row `cancelled`, drops it from `pending`, signals a running attempt, and — for a job cancelled while still queued — calls `_finished()` itself, because the worker would never report it. That is what already ends a flow whose queued stage is cancelled.
- `Library.interrupt_running()` (called from both `Library.initialize()` and `MediaQueue.close()`) marks running jobs `interrupted`, queued `qa` jobs `interrupted`, running flows `interrupted`, and pending QA messages failed.
- `Library.retry_job()` accepts `failed|cancelled|interrupted`, rejects a duplicate queued/running job for the same `(video, format, audio)`, and resets `status='queued', stage='queued', progress/error NULL`. Resumption is therefore already incremental: `StageStore` resumes verified checkpoints, `publish_attempt` reuses an asset whose fingerprint matches, and an identical finished translation is reused (`_after_subtitles` short-circuits to export).
- The UI keeps `flowId` only in browser memory, so after a reload the panel always says 「尚未開始一鍵流程」 and the `interrupted` flow has nowhere to appear.

## 4. Approach chosen

**Flow-level retry that reopens the same `flows` row (option A).**

The alternative of creating a new flow row per retry would have to re-derive the stage list from previously published artifacts and would lose the per-attempt error history; the alternative of only exposing the existing per-job retry endpoint cannot recover a flow ended by `flow_failed`, because `advance()` early-returns for a non-running flow. Reopening the row keeps a single audit trail, keeps `advance()` untouched as the only state machine, and reuses the existing resumption machinery for free.

## 5. Design

### 5.1 Storage (`storage.py`)

`Library.reopen_flow(flow_id: str) -> Record`

- Revives a flow in `failed|cancelled|interrupted` by setting `status='running'`, `error_code=NULL`, `updated_at=now`.
- Is idempotent for a `running` flow: returns it unchanged.
- Rejects `completed` with `ValueError("Flow already finished")`.
- Runs inside `BEGIN IMMEDIATE` and raises `ValueError("Flow already in progress")` when the same video already has another `running` flow, matching `create_flow`'s semantics so the partial unique index can never surface as an `IntegrityError`.
- Raises `ValueError` for an unknown `flow_id`.

`Library.latest_flow(video_id: str) -> Record | None`

- Returns the newest flow for the video (`ORDER BY created_at DESC, id DESC LIMIT 1`), in any status, or `None`.
- Calls `self.get_video(video_id)` first, like `flows()` and `active_flow()`.

### 5.2 Flow service (`flows.py`)

A second binding is added next to the existing enqueue hook, so the service still does not depend on `MediaQueue`:

```python
flows.bind(queue.enqueue)          # unchanged
flows.bind_cancel(queue.cancel)    # new
```

`FlowService.bind_cancel(cancel: Callable[[str], None]) -> None` stores the callback in `self._cancel`, defaulting to `None`. `cancel()` raises `FlowError("取消功能尚未啟用")` when the hook is unbound, so a service built without a queue cannot silently mark a flow cancelled while its child process keeps running.

`FlowService.cancel(flow_id: str) -> Record`

1. Load the flow. Unknown id → `KeyError`/`ValueError` propagates (HTTP 404).
2. Any status other than `running` → `FlowError("這條流程已經結束")`.
3. Find the job whose `kind == flow.stage` among `flow_jobs(flow_id)`.
   - Present and `queued|running` → `self._cancel(str(job["id"]))`. `MediaQueue.cancel` terminates a running child through its cancel event and finishes a queued job itself, which cascades through `advance()` to `status='cancelled'`.
   - Present but already in a terminal state → the direct update of the absent case, since there is nothing left to cancel.
   - Absent → update the flow directly to `status='cancelled'`, `error='cancelled'` (the stage never produced a job to cancel).
4. Return `self.status(flow_id)`.

`FlowService.retry(flow_id: str) -> Record`

All validation happens before any write, so a rejected retry leaves the row untouched.

1. Load the flow. Unknown id propagates.
2. `running` → `FlowError("這條流程仍在進行")`; `completed` → `FlowError("這條流程已經完成")`.
3. Find the job whose `kind == flow.stage`. Absent → `FlowError("這條流程沒有可重試的階段工作，請重新開始")`, because reopening it would leave a `running` flow with no work, blocking `flows_one_running`.
4. Preconditions carried over from the job-level route: a `translation` stage without `settings.gemini_api_key` → `FlowKeyError(MISSING_KEY_MESSAGE)`; `error_code == "container_confirmation_required"` → `FlowError` with the existing MKV text.
5. `self.library.reopen_flow(flow_id)` **first**, so a fast worker cannot finish the job before the flow accepts it.
6. Branch on the stage job's status:
   - `failed|cancelled|interrupted` → `self.library.retry_job(job_id)` then `self.enqueue(self.library.get_job(job_id))`.
   - `completed` (the `flow_failed` case) → no job is re-run; call `self.advance(flow_id, job)` to redo the chaining that raised.
7. Return `self.status(flow_id)`.

`FlowService.confirm()` gains one key, `"latest_flow_id": str | None`, from `Library.latest_flow(video_id)`. No other confirm field changes, and `status()` keeps its current shape.

### 5.3 HTTP API (`app.py`)

| Route | Success | Errors |
| --- | --- | --- |
| `POST /api/flows/{flow_id}/cancel` | 200 + `flows.status(flow_id)` | 404 「流程不存在」; 409 「這條流程已經結束」 |
| `POST /api/flows/{flow_id}/retry` | 200 + `flows.status(flow_id)` | 404 「流程不存在」; 409 for running / completed / no stage job / missing Gemini key / `container_confirmation_required` |
| `GET /api/videos/{video_id}/flow` | adds `latest_flow_id` | unchanged |
| `GET /api/flows/{flow_id}` | unchanged | unchanged |

Both new routes are POST to match the existing `/api/jobs/{id}/cancel|retry` pair, and return the same payload shape as `GET /api/flows/{id}` so the UI needs no follow-up request. They need no extra redaction: the flow's snapshot was already passed through `public_text` at start time (as `start_flow` does today), and `status()` carries no other free text — matching the current `GET /api/flows/{flow_id}` route.

### 5.4 Browser UI (`index.html`, `workspace.js`)

Markup — a new action block between the stage list and the artifact line:

```html
<div id="flow-actions" class="button-row" hidden>
  <button id="cancel-flow" type="button">取消流程</button>
  <button id="retry-flow" type="button">重試流程</button>
</div>
```

`renderFlowStatus()`

- `interrupted` appends 「· 已中斷，需手動重試」 to the summary line; `failed` appends `jobErrorLabel(flow.error_code)` when a label exists.
- The artifact link is rendered exactly as today, so finished exports stay downloadable.
- `flow-actions` stays hidden when `flowStatus` is `null`, shows only 「取消流程」 while `running`, only 「重試流程」 for `failed|cancelled|interrupted`, and nothing for `completed`. It also hides 「重試流程」 when `error_code === "container_confirmation_required"`, matching the job list, which already refuses to offer that dead end.

Handlers

- Cancel: `POST /api/flows/{id}/cancel`, adopt the response as `flowStatus`, `renderFlow()`, `pollJobs()`, re-enable on failure after `showError()`.
- Retry: `POST /api/flows/{id}/retry`, same treatment.
- Both disable their button while the request is in flight.

`requestFlowConfirm()` / `refreshFlow()`

- After a reload `flowId` is `null`; when the confirm payload carries `latest_flow_id`, adopt it, then fetch `/api/flows/{id}` as usual. A restarted service therefore shows the `interrupted` flow with its 「重試流程」 button.
- When `flowId` is still `null`, `flowStatus` stays `null` and the panel keeps saying 「尚未開始一鍵流程」.
- When the adopted flow is in a terminal state, polling stops re-requesting `/api/flows/{id}`; a successful retry returns `running` and polling resumes.

Unchanged

- `updateStartButton()` keeps trusting the server's `busy`/`blocked_reason`, so 「開始處理」 still creates a *new* flow after a flow has ended, exactly as today.
- Switching videos still clears `flowId`/`flowStatus` and re-adopts that video's latest flow.
- The per-job 「取消」/「重試」 buttons and their rules are untouched.

### 5.5 Error handling

- Every flow-level check runs before the first write, so a rejected cancel or retry changes nothing in the database and returns the existing Chinese wording.
- `reopen_flow` before `retry_job`/`enqueue` is what keeps `advance()` eligible when the worker finishes quickly.
- A concurrent second client starting a flow while a retry is running is still answered with the existing `BUSY_MESSAGE` (409).
- Cancel deliberately adds no flow-level termination logic: the existing `MediaQueue.cancel` cascade plus `advance()` already produces `status='cancelled'`, `error_code='cancelled'`, and the flow-level path only has to cover the "stage has no job yet" hole.
- A cancel or retry against an unknown flow keeps returning 404; the UI disables the pressed button in the meantime.

## 6. Tests

Added to `tests/test_workspace_flow_api.py` unless noted; `tests/test_workspace_ui.py` gets the static contract additions.

1. **Failed stage and reason survive with published versions** — a failing export stage leaves `flow.status == "failed"`, the stage list carrying the failed stage's `error_code`, and the previously completed subtitle version and asset still present.
2. **Retry does not download or translate again** — fail a middle stage, `POST .../retry`, and assert `adapter.downloads` is unchanged across the retry, `FlowAdapter.subtitles_downloaded` is unchanged, and `FakeTranslation` is not called again because the finished translation is reused.
3. **Cancel terminates a running child process** — park the export stage inside `FakeFFmpeg(block=True, ...)` (the `test_workspace_burned_export.py` harness), `POST .../cancel`, and assert the flow becomes `cancelled` while the ffmpeg `cancelled` event is observed within the timeout; a previously finished artifact stays downloadable.
4. **Duplicate start is 409, and a new flow is allowed after an end** — the existing covered case plus a new assertion that a fresh start after a `failed`/`cancelled` flow yields a different `flow_id`.
5. **Restart marks the flow as needing a manual retry** — after `interrupt_running()`, `GET /api/videos/{id}/flow` returns that flow's id in `latest_flow_id` with `status == "interrupted"`, and `POST .../retry` drives it back to `completed` without a new download.
6. **Storage and service contracts** — `reopen_flow` is idempotent for `running`, refuses `completed`, and refuses when another flow already runs for the video; `retry` answers 409 for `running`, `completed` and no-stage-job without mutating the row; `cancel` answers 409 for a terminal flow and 404 for an unknown id.
7. **UI contract** — `index.html` contains `flow-actions`, `cancel-flow` and `retry-flow`; `workspace.js` contains 「取消流程」, 「重試流程」, 「需手動重試」 and `latest_flow_id`, and still contains no `innerHTML`.
8. **Quality gates** — `ruff check`, `ruff format --check`, `mypy`, `pytest` (baseline 692 passed plus the new tests).

## 7. Acceptance criteria mapping

| Issue #7 criterion | Covered by |
| --- | --- |
| 失敗時顯示失敗階段與原因，保留已發佈版本 | §5.4 status text, §6.1 |
| 從失敗階段重試，不重新下載、不重新翻譯 | §5.2 `retry`, §6.2 |
| 可取消整條流程並終止進行中子程序 | §5.2 `cancel`, §5.3 route, §6.3 |
| 重複開始回 409 | §6.4 (existing behaviour kept) |
| 重啟後未完成流程標示需手動重試 | §5.1 `latest_flow`, §5.2 `confirm`, §5.4 adoption, §6.5 |
| HTTP API 測試 | §6.1–§6.6 |

## 8. Risks

- **Adopting `latest_flow_id` shows an old flow.** A user who starts processing, reloads, and then starts a *new* flow would briefly see the old one — but only until the start response replaces `flowStatus`. Accepted: the panel explicitly reports the status of the flow it shows.
- **`reopen_flow` versus `flows_one_running`.** Handled by checking inside `BEGIN IMMEDIATE`; the window between check and update is closed by the transaction.
- **A retry of `flow_failed` re-runs chaining, not the stage.** Chaining is deterministic, so a repeated failure re-fails the flow with the same error code rather than looping; the user sees the same message and can fall back to 「開始處理」.
