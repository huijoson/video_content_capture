"""Versioned local metadata and confined, immutable artifact publication.

Managed paths reject traversal and existing symlinks. Private creation modes
protect new storage from other users. This is not a sandbox against malicious
processes running as the same OS user: SQLite opens by pathname, and publication
still uses a temporary name for its final hard link. Such processes can race these
operations. Descriptor traversal pins artifact parent directories against swaps.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from video_content_capture.redaction import scrub_text
from video_content_capture.workspace.subtitles import Cue, validate_cues, validate_language

SCHEMA_VERSION = 7
# One-click flow stages reuse the existing job kinds, in this fixed order.
FLOW_STAGES = ("download", "subtitles", "translation", "export")
FLOW_ENDED = ("completed", "failed", "cancelled", "interrupted")
Record = dict[str, object]
_VIDEO_ID = re.compile(r"[0-9a-f]{32}\Z")
_VIDEO_SUBDIRECTORIES = ("source", "subtitles", "previews", "exports")
_PREVIEW_FILE = re.compile(r"[0-9a-f]{32}\.mp4\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Library:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.db_path = self.root / "library.sqlite3"

    def initialize(self) -> None:
        """Create or migrate metadata, then mark unfinished attempts interrupted."""
        for component in (self.root, *self.root.parents):
            if component.is_symlink():
                raise ValueError("Library path must not contain symbolic links")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.db_path = self.root / "library.sqlite3"
        with self._directory(Path("videos"), create=True):
            pass
        existed = self.db_path.exists()
        if self.db_path.is_symlink():
            raise ValueError("Database must not be a symbolic link")
        if not existed:
            descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with self._connect() as connection:
            self.migrate(connection, backup=existed)
        self.interrupt_running()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.db_path.is_symlink():
            raise ValueError("Database must not be a symbolic link")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    def migrate(self, connection: sqlite3.Connection, *, backup: bool = True) -> None:
        """Migration entry point; preserve a consistent snapshot before each upgrade."""
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise ValueError("Library schema is newer than this application supports")
        if version == SCHEMA_VERSION:
            return
        if backup:
            backup_path = self.db_path.with_name(f"{self.db_path.name}.backup-{uuid4().hex}")
            descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            try:
                with closing(sqlite3.connect(backup_path)) as destination:
                    connection.backup(destination)
            except Exception:
                backup_path.unlink(missing_ok=True)
                raise
        connection.execute("BEGIN IMMEDIATE")
        if version == 0:
            connection.execute(
                "CREATE TABLE videos (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "status TEXT NOT NULL, attempt_id TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        if version < 2:
            for definition in (
                "youtube_id TEXT UNIQUE",
                "duration REAL",
                "source_url TEXT",
                "metadata TEXT",
                "position REAL NOT NULL DEFAULT 0",
                "deleting INTEGER NOT NULL DEFAULT 0",
            ):
                # SQLite cannot add a UNIQUE column; create its index separately.
                connection.execute(
                    f"ALTER TABLE videos ADD COLUMN {definition.replace(' UNIQUE', '')}"
                )
            connection.execute("CREATE UNIQUE INDEX videos_youtube_id ON videos (youtube_id)")
            for definition in (
                "video_id TEXT REFERENCES videos(id)",
                "stage TEXT NOT NULL DEFAULT 'queued'",
                "progress REAL",
                "error_code TEXT",
                "format_id TEXT",
                "audio_id TEXT",
                "deleting INTEGER NOT NULL DEFAULT 0",
            ):
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {definition}")
            connection.execute(
                "CREATE TABLE media_assets (id TEXT PRIMARY KEY, video_id TEXT NOT NULL "
                "REFERENCES videos(id), format_id TEXT NOT NULL, audio_id TEXT NOT NULL, "
                "path TEXT NOT NULL, checksum TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "browser_playable INTEGER NOT NULL, container TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE job_stages (job_id TEXT NOT NULL REFERENCES jobs(id), "
                "name TEXT NOT NULL, path TEXT NOT NULL, checksum TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, PRIMARY KEY(job_id,name))"
            )
        if version < 3:
            for definition in (
                "playback_version_id TEXT",
                "translation_source_version_id TEXT",
                "export_version_id TEXT",
                "qa_version_id TEXT",
            ):
                connection.execute(f"ALTER TABLE videos ADD COLUMN {definition}")
            connection.execute("ALTER TABLE jobs ADD COLUMN snapshot TEXT")
            connection.execute(
                "CREATE TABLE subtitle_versions (id TEXT PRIMARY KEY, video_id TEXT NOT NULL "
                "REFERENCES videos(id), source_type TEXT NOT NULL, language TEXT NOT NULL, "
                "name TEXT NOT NULL, parent_id TEXT REFERENCES subtitle_versions(id), "
                "content_hash TEXT NOT NULL, complete INTEGER NOT NULL, fingerprint TEXT, "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE subtitle_cues (version_id TEXT NOT NULL "
                "REFERENCES subtitle_versions(id), "
                "id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL, "
                "ordinal INTEGER NOT NULL, PRIMARY KEY(version_id,id))"
            )
            connection.execute(
                "CREATE TABLE export_artifacts (id TEXT PRIMARY KEY, video_id TEXT NOT NULL "
                "REFERENCES videos(id), job_id TEXT NOT NULL REFERENCES jobs(id), "
                "path TEXT NOT NULL, container TEXT NOT NULL, summary TEXT NOT NULL, "
                "checksum TEXT NOT NULL)"
            )
        if version < 4:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(videos)")}
            if "last_conversation_id" not in columns:
                connection.execute("ALTER TABLE videos ADD COLUMN last_conversation_id TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, "
                "video_id TEXT NOT NULL REFERENCES videos(id), source_version_id TEXT NOT NULL "
                "REFERENCES subtitle_versions(id), created_at TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'active')"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS qa_messages (id TEXT PRIMARY KEY, "
                "conversation_id TEXT NOT NULL REFERENCES conversations(id), "
                "question TEXT NOT NULL, "
                "response_json TEXT, status TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES "
                "jobs(id), attempt_id TEXT, model TEXT NOT NULL, in_memory INTEGER NOT NULL "
                "DEFAULT 0, memory_ids TEXT NOT NULL DEFAULT '[]', error_code TEXT, "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS qa_one_pending ON qa_messages(conversation_id) "
                "WHERE status='pending'"
            )
        if version < 5:
            # Rebuildable browser previews live apart from source assets and export artifacts.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS media_previews (id TEXT PRIMARY KEY, "
                "video_id TEXT NOT NULL REFERENCES videos(id), asset_id TEXT NOT NULL "
                "REFERENCES media_assets(id), job_id TEXT NOT NULL REFERENCES jobs(id), "
                "path TEXT NOT NULL, checksum TEXT NOT NULL, height INTEGER NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS media_previews_asset ON media_previews(asset_id)"
            )
        if version < 6:
            # Artifacts published before burned-in export all carry selectable tracks.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(export_artifacts)")}
            if "subtitle_form" not in columns:
                connection.execute(
                    "ALTER TABLE export_artifacts ADD COLUMN subtitle_form TEXT NOT NULL "
                    "DEFAULT 'tracks'"
                )
        if version < 7:
            # One-click flows coordinate the existing jobs; the job row carries its flow.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS flows (id TEXT PRIMARY KEY, "
                "video_id TEXT NOT NULL REFERENCES videos(id), status TEXT NOT NULL, "
                "stage TEXT NOT NULL, snapshot TEXT NOT NULL, error_code TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS flows_one_running ON flows(video_id) "
                "WHERE status = 'running'"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "flow_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN flow_id TEXT REFERENCES flows(id)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _record(cursor: sqlite3.Cursor) -> Record:
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Record not found")
        return dict(zip((column[0] for column in cursor.description), row, strict=True))

    @staticmethod
    def _records(cursor: sqlite3.Cursor) -> list[Record]:
        keys = [column[0] for column in cursor.description]
        return [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]

    def import_video(
        self, youtube_id: str, title: str, duration: float, source_url: str, metadata: str
    ) -> Record:
        json.loads(metadata)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)
            )
            if existing.fetchone() is None:
                connection.execute(
                    "INSERT INTO videos (id,title,created_at,updated_at,youtube_id,duration,"
                    "source_url,metadata) VALUES (?,?,?,?,?,?,?,?)",
                    (uuid4().hex, title, now, now, youtube_id, duration, source_url, metadata),
                )
            result = self._record(
                connection.execute(
                    "SELECT * FROM videos WHERE youtube_id = ? AND deleting = 0", (youtube_id,)
                )
            )
        self.video_dir(str(result["id"]))
        return result

    def get_video(self, video_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT * FROM videos WHERE id = ? AND deleting = 0", (video_id,)
                )
            )

    def list_videos(self) -> list[Record]:
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM videos WHERE deleting = 0 ORDER BY created_at DESC, id"
                )
            )

    def refresh_metadata(self, video_id: str, metadata: str) -> None:
        json.loads(metadata)
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE videos SET metadata = ?, updated_at = ? WHERE id = ? AND deleting = 0",
                    (metadata, datetime.now(UTC).isoformat(), video_id),
                ).rowcount
                != 1
            ):
                raise ValueError("Video not found")

    def set_position(self, video_id: str, position: float) -> None:
        if not math.isfinite(position) or position < 0:
            raise ValueError("Invalid playback position")
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE videos SET position = ?, updated_at = ? WHERE id = ? AND deleting = 0 "
                    "AND (duration IS NULL OR ? <= duration)",
                    (position, datetime.now(UTC).isoformat(), video_id, position),
                ).rowcount
                != 1
            ):
                raise ValueError("Invalid playback position or video not found")

    def mark_deleting(self, video_id: str) -> list[str]:
        """Make the video unwritable first; return jobs whose workers must be stopped."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE video_id=? AND status IN ('queued','running') "
                    "ORDER BY created_at,id",
                    (video_id,),
                )
            ]
            connection.execute("UPDATE videos SET deleting = 1 WHERE id = ?", (video_id,))
            connection.execute(
                "UPDATE qa_messages SET status='cancelled',response_json=NULL,in_memory=0,"
                "memory_ids='[]' WHERE status='pending' AND conversation_id IN "
                "(SELECT id FROM conversations WHERE video_id=?)",
                (video_id,),
            )
            connection.execute(
                "UPDATE jobs SET deleting = 1, status = CASE WHEN status IN ('queued','running') "
                "THEN 'cancelled' ELSE status END WHERE video_id = ?",
                (video_id,),
            )
        return active

    def create_job(
        self, video_id: str, format_id: str, audio_id: str, flow_id: str | None = None
    ) -> Record:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute(
                    "SELECT * FROM videos WHERE id = ? AND deleting = 0", (video_id,)
                )
            )
            cursor = connection.execute(
                "SELECT * FROM jobs WHERE video_id = ? AND kind = 'download' "
                "AND status IN ('queued','running') AND deleting = 0 "
                "AND format_id = ? AND audio_id = ? ORDER BY created_at LIMIT 1",
                (video_id, format_id, audio_id),
            )
            row = cursor.fetchone()
            if row is not None:
                # Reuse an equivalent download, but honour the caller's flow: a job left
                # `queued` by a crash has no flow, and returning it unlinked would strand
                # the new flow running forever because the completion hook ignores it.
                existing = dict(zip((column[0] for column in cursor.description), row, strict=True))
                if flow_id is None or str(existing["flow_id"]) == str(flow_id):
                    return existing
                connection.execute(
                    "UPDATE jobs SET flow_id = ?, updated_at = ? WHERE id = ?",
                    (flow_id, now, existing["id"]),
                )
                return self._record(
                    connection.execute("SELECT * FROM jobs WHERE id = ?", (existing["id"],))
                )
            job_id = uuid4().hex
            connection.execute(
                "INSERT INTO jobs (id,kind,status,created_at,updated_at,video_id,"
                "format_id,audio_id,flow_id) "
                "VALUES (?,'download','queued',?,?,?,?,?,?)",
                (job_id, now, now, video_id, format_id, audio_id, flow_id),
            )
            return self._record(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def create_snapshot_job(
        self, video_id: str, kind: str, snapshot: Record, flow_id: str | None = None
    ) -> Record:
        if kind not in {"subtitles", "translation", "export", "qa", "preview"}:
            raise ValueError("Invalid job kind")
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        if scrub_text(encoded) != encoded:
            raise ValueError("Job snapshot contains sensitive data")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute(
                    "SELECT * FROM videos WHERE id = ? AND deleting = 0", (video_id,)
                )
            )
            job_id = uuid4().hex
            connection.execute(
                "INSERT INTO jobs (id,kind,status,created_at,updated_at,video_id,snapshot,"
                "flow_id) VALUES (?,?,'queued',?,?,?,?,?)",
                (job_id, kind, now, now, video_id, encoded, flow_id),
            )
            return self._record(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def get_job(self, job_id: str) -> Record:
        with self._connect() as connection:
            return self._record(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def list_jobs(self) -> list[Record]:
        with self._connect() as connection:
            return self._records(connection.execute("SELECT * FROM jobs ORDER BY created_at, id"))

    def start_job(self, job_id: str) -> str:
        attempt = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # QA has its own lane so answers stay available while media work continues.
            running = connection.execute(
                "SELECT 1 FROM jobs current JOIN jobs other ON other.status = 'running' "
                "AND (other.kind = 'qa') = (current.kind = 'qa') WHERE current.id = ?",
                (job_id,),
            ).fetchone()
            if running is not None:
                raise ValueError("Another job is running in this lane")
            changed = connection.execute(
                "UPDATE jobs SET status = 'running', attempt_id = ?, stage = 'download', "
                "progress = NULL, error_code = NULL, updated_at = ? WHERE id = ? "
                "AND status = 'queued' AND deleting = 0 AND EXISTS "
                "(SELECT 1 FROM videos WHERE videos.id = jobs.video_id "
                "AND videos.deleting = 0)",
                (attempt, datetime.now(UTC).isoformat(), job_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Job is not queued")
            connection.execute(
                "UPDATE qa_messages SET attempt_id=? WHERE request_id=? AND status='pending'",
                (attempt, job_id),
            )
        return attempt

    def cancel_job(self, job_id: str) -> None:
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? "
                    "AND status IN ('queued','running')",
                    (datetime.now(UTC).isoformat(), job_id),
                ).rowcount
                != 1
            ):
                raise ValueError("Job cannot be cancelled")
            connection.execute(
                "UPDATE qa_messages SET status='cancelled',response_json=NULL,in_memory=0,"
                "memory_ids='[]' WHERE request_id=? AND status='pending'",
                (job_id,),
            )

    def retry_job(self, job_id: str) -> Record:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            qa = connection.execute(
                "SELECT conversation_id FROM qa_messages WHERE request_id=?", (job_id,)
            ).fetchone()
            if qa is not None:
                self._record(
                    connection.execute(
                        "SELECT * FROM conversations WHERE id=? AND status='active'", (qa[0],)
                    )
                )
                if connection.execute(
                    "SELECT 1 FROM qa_messages WHERE conversation_id=? AND status='pending'",
                    (qa[0],),
                ).fetchone():
                    raise ValueError("Conversation has a pending request")
            duplicate = connection.execute(
                "SELECT 1 FROM jobs current JOIN jobs other ON "
                "current.video_id=other.video_id AND current.format_id=other.format_id "
                "AND current.audio_id=other.audio_id WHERE current.id=? AND other.id!=? "
                "AND other.status IN ('queued','running') AND other.deleting=0",
                (job_id, job_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("Matching job is already active")
            if (
                connection.execute(
                    "UPDATE jobs SET status = 'queued', stage = 'queued', progress = NULL, "
                    "error_code = NULL, updated_at = ? WHERE id = ? AND deleting = 0 "
                    "AND status IN ('failed','cancelled','interrupted') AND EXISTS "
                    "(SELECT 1 FROM videos WHERE videos.id = jobs.video_id "
                    "AND videos.deleting = 0)",
                    (datetime.now(UTC).isoformat(), job_id),
                ).rowcount
                != 1
            ):
                raise ValueError("Job cannot be retried")
            connection.execute(
                "UPDATE qa_messages SET status='pending',attempt_id=NULL,response_json=NULL,"
                "in_memory=0,memory_ids='[]',error_code=NULL WHERE request_id=?",
                (job_id,),
            )
            return self._record(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def update_attempt(
        self,
        job_id: str,
        attempt: str,
        stage: str,
        progress: float | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET stage = ?, progress = ?, error_code = ?, status = ?, "
                "updated_at = ? WHERE id = ? AND attempt_id = ? AND status = 'running' "
                "AND deleting = 0",
                (
                    stage,
                    progress,
                    error,
                    "failed" if error else "running",
                    datetime.now(UTC).isoformat(),
                    job_id,
                    attempt,
                ),
            )
            if error:
                connection.execute(
                    "UPDATE qa_messages SET status='failed',response_json=NULL,in_memory=0,"
                    "memory_ids='[]',error_code=? WHERE request_id=? AND attempt_id=? "
                    "AND status='pending' AND EXISTS (SELECT 1 FROM jobs WHERE id=? "
                    "AND attempt_id=? AND status='failed' AND deleting=0)",
                    (scrub_text(error), job_id, attempt, job_id, attempt),
                )

    def publish_attempt(
        self,
        job_id: str,
        attempt: str,
        callback: Callable[[sqlite3.Connection], object],
        owner_validator: Callable[[sqlite3.Connection], bool] | None = None,
        *,
        complete: bool = True,
    ) -> bool:
        """Fence stale workers and atomically publish metadata for S2 and later stages."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            valid = connection.execute(
                "SELECT 1 FROM jobs JOIN videos ON videos.id = jobs.video_id "
                "WHERE jobs.id = ? AND jobs.attempt_id = ? AND jobs.status = 'running' "
                "AND jobs.deleting = 0 AND videos.deleting = 0",
                (job_id, attempt),
            ).fetchone()
            if valid is None or (owner_validator is not None and not owner_validator(connection)):
                return False
            callback(connection)
            if complete:
                connection.execute(
                    "UPDATE jobs SET status = 'completed', stage = 'completed', progress = 1, "
                    "updated_at = ? WHERE id = ?",
                    (datetime.now(UTC).isoformat(), job_id),
                )
            return True

    def stage_records(self, job_id: str) -> list[Record]:
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM job_stages WHERE job_id = ? ORDER BY name",
                    (job_id,),
                )
            )

    def publish_stage(
        self,
        job_id: str,
        attempt: str,
        name: str,
        path: str,
        checksum: str,
        fingerprint: str,
        writer: Callable[[sqlite3.Connection], object],
    ) -> bool:
        """Keep verified stage output across manual retries without completing its job."""

        def checkpoint(connection: sqlite3.Connection) -> None:
            writer(connection)
            connection.execute(
                "INSERT INTO job_stages (job_id,name,path,checksum,fingerprint) VALUES (?,?,?,?,?) "
                "ON CONFLICT(job_id,name) DO UPDATE SET path = excluded.path, "
                "checksum = excluded.checksum, fingerprint = excluded.fingerprint",
                (job_id, name, path, checksum, fingerprint),
            )

        return self.publish_attempt(job_id, attempt, checkpoint, complete=False)

    def release_stages(self, connection: sqlite3.Connection, job_id: str) -> list[Path]:
        """Drop a completed job's stage rows inside its publish transaction.

        Returns the stage files to remove once that transaction has committed.
        """
        paths = [
            Path(str(row[0]))
            for row in connection.execute("SELECT path FROM job_stages WHERE job_id=?", (job_id,))
        ]
        connection.execute("DELETE FROM job_stages WHERE job_id=?", (job_id,))
        return paths

    def remove_stage_files(self, paths: list[Path]) -> None:
        """Best-effort unlink of released stage files; never follows links."""
        for relative in paths:
            try:
                with self._directory(relative.parent) as directory:
                    os.unlink(relative.name, dir_fd=directory)
            except (ValueError, OSError):
                pass

    def assets(self, video_id: str) -> list[Record]:
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT media_assets.* FROM media_assets JOIN videos ON videos.id = video_id "
                    "WHERE video_id = ? AND videos.deleting = 0",
                    (video_id,),
                )
            )

    def get_asset(self, asset_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT media_assets.* FROM media_assets JOIN videos ON videos.id = video_id "
                    "WHERE media_assets.id = ? AND videos.deleting = 0",
                    (asset_id,),
                )
            )

    def subtitle_versions(self, video_id: str) -> list[Record]:
        self.get_video(video_id)
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM subtitle_versions WHERE video_id = ? ORDER BY created_at,id",
                    (video_id,),
                )
            )

    def get_subtitle_version(self, version_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT subtitle_versions.* FROM subtitle_versions JOIN videos "
                    "ON videos.id=subtitle_versions.video_id WHERE subtitle_versions.id=? "
                    "AND videos.deleting=0",
                    (version_id,),
                )
            )

    def subtitle_cues(self, version_id: str) -> list[Cue]:
        self.get_subtitle_version(version_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,start,end,text FROM subtitle_cues WHERE version_id=? ORDER BY ordinal",
                (version_id,),
            )
            return [Cue(id=row[0], start=row[1], end=row[2], text=row[3]) for row in rows]

    @staticmethod
    def _write_cues(connection: sqlite3.Connection, version_id: str, cues: list[Cue]) -> str:
        encoded = json.dumps([cue.model_dump() for cue in cues], sort_keys=True, ensure_ascii=False)
        connection.executemany(
            "INSERT INTO subtitle_cues (version_id,id,start,end,text,ordinal) VALUES (?,?,?,?,?,?)",
            [
                (version_id, cue.id, cue.start, cue.end, cue.text, index)
                for index, cue in enumerate(cues)
            ],
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def create_subtitle_version(
        self,
        video_id: str,
        language: str,
        name: str,
        source_type: str,
        cues: list[Cue],
        parent_id: str | None = None,
        complete: bool = True,
        fingerprint: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Record:
        if connection is None:
            with self._connect() as owned:
                owned.execute("BEGIN IMMEDIATE")
                return self.create_subtitle_version(
                    video_id,
                    language,
                    name,
                    source_type,
                    cues,
                    parent_id,
                    complete,
                    fingerprint,
                    owned,
                )
        video = self._record(
            connection.execute("SELECT * FROM videos WHERE id=? AND deleting=0", (video_id,))
        )
        validate_language(language)
        if source_type not in {"platform_manual", "platform_auto", "asr", "import", "translation"}:
            raise ValueError("Invalid subtitle source")
        if not name.strip():
            raise ValueError("Subtitle version name is required")
        if parent_id is not None:
            parent = self._record(
                connection.execute(
                    "SELECT * FROM subtitle_versions WHERE id=? AND video_id=? AND complete=1",
                    (parent_id, video_id),
                )
            )
            if parent["video_id"] != video_id:
                raise ValueError("Invalid parent version")
        checked = validate_cues(cues, float(str(video["duration"]))).cues if cues else []
        if complete and not checked:
            raise ValueError("Complete subtitle needs cues")
        version_id = uuid4().hex
        connection.execute(
            "INSERT INTO subtitle_versions "
            "(id,video_id,source_type,language,name,parent_id,content_hash,"
            "complete,fingerprint,created_at) "
            "VALUES (?,?,?,?,?,?,?,0,?,?)",
            (
                version_id,
                video_id,
                source_type,
                language,
                scrub_text(name),
                parent_id,
                "",
                fingerprint,
                datetime.now(UTC).isoformat(),
            ),
        )
        content_hash = self._write_cues(connection, version_id, checked)
        connection.execute(
            "UPDATE subtitle_versions SET content_hash=?,complete=? WHERE id=?",
            (content_hash, int(complete), version_id),
        )
        return self._record(
            connection.execute("SELECT * FROM subtitle_versions WHERE id=?", (version_id,))
        )

    def complete_subtitle_version(
        self, connection: sqlite3.Connection, version_id: str, cues: list[Cue]
    ) -> None:
        version = self._record(
            connection.execute(
                "SELECT subtitle_versions.*,videos.duration FROM subtitle_versions JOIN videos "
                "ON videos.id=subtitle_versions.video_id WHERE subtitle_versions.id=? "
                "AND subtitle_versions.complete=0 AND videos.deleting=0",
                (version_id,),
            )
        )
        checked = validate_cues(cues, float(str(version["duration"]))).cues
        if connection.execute(
            "SELECT 1 FROM subtitle_cues WHERE version_id=?", (version_id,)
        ).fetchone():
            raise ValueError("Incomplete version already contains cues")
        content_hash = self._write_cues(connection, version_id, checked)
        connection.execute(
            "UPDATE subtitle_versions SET content_hash=?,complete=1 WHERE id=?",
            (content_hash, version_id),
        )

    def set_subtitle_selection(self, video_id: str, selection: str, version_id: str | None) -> None:
        columns = {
            "playback": "playback_version_id",
            "translation_source": "translation_source_version_id",
            "export": "export_version_id",
            "qa": "qa_version_id",
        }
        if selection not in columns:
            raise ValueError("Invalid subtitle selection")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute("SELECT * FROM videos WHERE id=? AND deleting=0", (video_id,))
            )
            if version_id is not None:
                self._record(
                    connection.execute(
                        "SELECT * FROM subtitle_versions WHERE id=? AND video_id=? AND complete=1",
                        (version_id, video_id),
                    )
                )
            connection.execute(
                f"UPDATE videos SET {columns[selection]}=? WHERE id=?", (version_id, video_id)
            )

    def exports(self, video_id: str) -> list[Record]:
        self.get_video(video_id)
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM export_artifacts WHERE video_id=? ORDER BY id", (video_id,)
                )
            )

    def get_export(self, export_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT export_artifacts.* FROM export_artifacts JOIN videos "
                    "ON videos.id=export_artifacts.video_id WHERE export_artifacts.id=? "
                    "AND videos.deleting=0",
                    (export_id,),
                )
            )

    def create_conversation(self, video_id: str, source_version_id: str | None = None) -> Record:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute("SELECT * FROM videos WHERE id=? AND deleting=0", (video_id,))
            )
            if source_version_id is None:
                source = self._record(
                    connection.execute(
                        "SELECT * FROM subtitle_versions WHERE video_id=? AND complete=1 "
                        "AND source_type IN ('platform_manual','platform_auto','asr') "
                        "ORDER BY created_at,id LIMIT 1",
                        (video_id,),
                    )
                )
                source_version_id = str(source["id"])
            self._record(
                connection.execute(
                    "SELECT * FROM subtitle_versions WHERE id=? AND video_id=? AND complete=1",
                    (source_version_id, video_id),
                )
            )
            conversation_id = uuid4().hex
            connection.execute(
                "INSERT INTO conversations(id,video_id,source_version_id,created_at) "
                "VALUES(?,?,?,?)",
                (conversation_id, video_id, source_version_id, datetime.now(UTC).isoformat()),
            )
            connection.execute(
                "UPDATE videos SET last_conversation_id=?,qa_version_id=? WHERE id=?",
                (conversation_id, source_version_id, video_id),
            )
            return self._record(
                connection.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,))
            )

    def ensure_conversation(self, video_id: str) -> Record | None:
        video = self.get_video(video_id)
        if video["last_conversation_id"]:
            return self.get_conversation(str(video["last_conversation_id"]))
        # Explicitly deleting a conversation does not silently create another one.
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE video_id=?", (video_id,)
            ).fetchone():
                return None
        try:
            return self.create_conversation(video_id)
        except ValueError:
            return None

    def get_conversation(self, conversation_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT conversations.* FROM conversations JOIN videos "
                    "ON videos.id=conversations.video_id WHERE conversations.id=? "
                    "AND conversations.status='active' AND videos.deleting=0",
                    (conversation_id,),
                )
            )

    def list_conversations(self, video_id: str) -> list[Record]:
        self.get_video(video_id)
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM conversations WHERE video_id=? AND status='active' "
                    "ORDER BY created_at,id",
                    (video_id,),
                )
            )

    def select_conversation(self, video_id: str, conversation_id: str) -> Record:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._record(
                connection.execute(
                    "SELECT conversations.* FROM conversations JOIN videos "
                    "ON videos.id=conversations.video_id WHERE conversations.id=? "
                    "AND conversations.video_id=? AND conversations.status='active' "
                    "AND videos.deleting=0",
                    (conversation_id, video_id),
                )
            )
            connection.execute(
                "UPDATE videos SET last_conversation_id=?,qa_version_id=? WHERE id=?",
                (conversation_id, conversation["source_version_id"], video_id),
            )
            return conversation

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._record(
                connection.execute(
                    "SELECT * FROM conversations WHERE id=? AND status='active'", (conversation_id,)
                )
            )
            connection.execute(
                "UPDATE conversations SET status='deleted' WHERE id=?", (conversation_id,)
            )
            connection.execute(
                "UPDATE jobs SET status='cancelled',deleting=1 WHERE id IN "
                "(SELECT request_id FROM qa_messages WHERE conversation_id=?) "
                "AND status IN ('queued','running')",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM qa_messages WHERE conversation_id=?", (conversation_id,)
            )
            replacement = connection.execute(
                "SELECT id,source_version_id FROM conversations WHERE video_id=? "
                "AND status='active' ORDER BY created_at DESC,id LIMIT 1",
                (conversation["video_id"],),
            ).fetchone()
            connection.execute(
                "UPDATE videos SET last_conversation_id=?,qa_version_id=? WHERE id=? "
                "AND last_conversation_id=?",
                (
                    replacement[0] if replacement else None,
                    replacement[1] if replacement else None,
                    conversation["video_id"],
                    conversation_id,
                ),
            )

    def create_qa_message(self, conversation_id: str, question: str, model: str) -> Record:
        question = scrub_text(question).strip()
        model = scrub_text(model).strip()
        if not question or not model:
            raise ValueError("Question and model are required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._record(
                connection.execute(
                    "SELECT conversations.* FROM conversations JOIN videos "
                    "ON videos.id=conversations.video_id JOIN subtitle_versions "
                    "ON subtitle_versions.id=conversations.source_version_id "
                    "WHERE conversations.id=? AND conversations.status='active' "
                    "AND videos.deleting=0 AND subtitle_versions.complete=1",
                    (conversation_id,),
                )
            )
            if connection.execute(
                "SELECT 1 FROM qa_messages WHERE conversation_id=? AND status='pending'",
                (conversation_id,),
            ).fetchone():
                raise ValueError("Conversation has a pending request")
            job_id, message_id, now = uuid4().hex, uuid4().hex, datetime.now(UTC).isoformat()
            snapshot = json.dumps(
                {
                    "video_id": conversation["video_id"],
                    "conversation_id": conversation_id,
                    "source_version_id": conversation["source_version_id"],
                    "message_id": message_id,
                    "model": model,
                }
            )
            connection.execute(
                "INSERT INTO jobs(id,kind,status,created_at,updated_at,video_id,snapshot) "
                "VALUES(?,'qa','queued',?,?,?,?)",
                (job_id, now, now, conversation["video_id"], snapshot),
            )
            connection.execute(
                "INSERT INTO qa_messages(id,conversation_id,question,status,request_id,model,"
                "created_at) VALUES(?,?,?,'pending',?,?,?)",
                (message_id, conversation_id, question, job_id, model, now),
            )
            return self._record(
                connection.execute("SELECT * FROM qa_messages WHERE id=?", (message_id,))
            )

    def get_qa_message(self, message_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute("SELECT * FROM qa_messages WHERE id=?", (message_id,))
            )

    def qa_messages(self, conversation_id: str) -> list[Record]:
        self.get_conversation(conversation_id)
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM qa_messages WHERE conversation_id=? ORDER BY created_at,id",
                    (conversation_id,),
                )
            )

    def qa_memory(self, conversation_id: str, limit: int = 8) -> list[Record]:
        messages = [
            message
            for message in self.qa_messages(conversation_id)
            if message["status"] == "completed" and message["response_json"] is not None
        ]
        return messages[-min(max(limit, 0), 8) :] if limit > 0 else []

    def publish_qa_message(
        self, job_id: str, attempt: str, response: Record, memory_ids: list[str]
    ) -> bool:
        job = self.get_job(job_id)
        snapshot = json.loads(str(job["snapshot"]))
        encoded = json.dumps(response, ensure_ascii=False)
        if scrub_text(encoded) != encoded:
            raise ValueError("Answer contains sensitive data")

        def valid(connection: sqlite3.Connection) -> bool:
            return (
                connection.execute(
                    "SELECT 1 FROM conversations JOIN qa_messages ON "
                    "qa_messages.conversation_id=conversations.id JOIN subtitle_versions ON "
                    "subtitle_versions.id=conversations.source_version_id WHERE conversations.id=? "
                    "AND conversations.video_id=? AND conversations.source_version_id=? "
                    "AND conversations.status='active' AND subtitle_versions.complete=1 "
                    "AND qa_messages.id=? AND qa_messages.request_id=? "
                    "AND qa_messages.attempt_id=? "
                    "AND qa_messages.status='pending'",
                    (
                        snapshot["conversation_id"],
                        snapshot["video_id"],
                        snapshot["source_version_id"],
                        snapshot["message_id"],
                        job_id,
                        attempt,
                    ),
                ).fetchone()
                is not None
            )

        def publish(connection: sqlite3.Connection) -> None:
            eligible = self._records(
                connection.execute(
                    "SELECT * FROM qa_messages WHERE conversation_id=? AND status='completed' "
                    "ORDER BY created_at,id",
                    (snapshot["conversation_id"],),
                )
            )
            valid_ids = {str(message["id"]) for message in eligible[-8:]}
            if (
                len(memory_ids) > 8
                or len(set(memory_ids)) != len(memory_ids)
                or not set(memory_ids) <= valid_ids
            ):
                raise ValueError("Invalid answer memory range")
            connection.execute(
                "UPDATE qa_messages SET status='completed',response_json=?,memory_ids=?,"
                "error_code=NULL WHERE id=?",
                (encoded, json.dumps(memory_ids), snapshot["message_id"]),
            )
            # in_memory marks the turns the next request may reference; memory_ids keeps
            # the range actually sent with this answer after token preflight.
            connection.execute(
                "UPDATE qa_messages SET in_memory=0 WHERE conversation_id=?",
                (snapshot["conversation_id"],),
            )
            connection.execute(
                "UPDATE qa_messages SET in_memory=1 WHERE id IN (SELECT id FROM qa_messages "
                "WHERE conversation_id=? AND status='completed' "
                "ORDER BY created_at DESC,id DESC LIMIT 8)",
                (snapshot["conversation_id"],),
            )

        return self.publish_attempt(job_id, attempt, publish, valid)

    def subtitle_version_referenced(self, version_id: str) -> bool:
        with self._connect() as connection:
            return self._subtitle_version_referenced(connection, version_id)

    @staticmethod
    def _subtitle_version_referenced(connection: sqlite3.Connection, version_id: str) -> bool:
        if (
            connection.execute(
                "SELECT 1 FROM conversations WHERE source_version_id=? AND status='active'",
                (version_id,),
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM subtitle_versions WHERE parent_id=?", (version_id,)
            ).fetchone()
        ):
            return True
        # Export snapshots retain the exact subtitle selection used for the artifact.
        rows = connection.execute(
            "SELECT jobs.snapshot FROM export_artifacts "
            "JOIN jobs ON jobs.id=export_artifacts.job_id"
        )
        return any(version_id in str(row[0]) for row in rows)

    def delete_subtitle_version(self, version_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute("SELECT * FROM subtitle_versions WHERE id=?", (version_id,))
            )
            if self._subtitle_version_referenced(connection, version_id):
                raise ValueError("Subtitle version is referenced")
            for column in (
                "playback_version_id",
                "translation_source_version_id",
                "export_version_id",
                "qa_version_id",
            ):
                connection.execute(
                    f"UPDATE videos SET {column}=NULL WHERE {column}=?", (version_id,)
                )
            # Deleted conversation tombstones (messages already removed) no longer pin a source.
            connection.execute(
                "DELETE FROM conversations WHERE source_version_id=? AND status='deleted'",
                (version_id,),
            )
            connection.execute("DELETE FROM subtitle_cues WHERE version_id=?", (version_id,))
            connection.execute("DELETE FROM subtitle_versions WHERE id=?", (version_id,))

    def create_preview_job(self, video_id: str, asset_id: str) -> Record:
        """Queue one explicit preview for an asset the browser cannot play directly."""
        snapshot = json.dumps({"asset_id": asset_id}, sort_keys=True)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            asset = self._record(
                connection.execute(
                    "SELECT media_assets.* FROM media_assets JOIN videos ON videos.id=video_id "
                    "WHERE media_assets.id=? AND video_id=? AND videos.deleting=0",
                    (asset_id, video_id),
                )
            )
            if asset["browser_playable"]:
                raise ValueError("Source is directly playable")
            if connection.execute(
                "SELECT 1 FROM media_previews WHERE asset_id=?", (asset_id,)
            ).fetchone():
                raise ValueError("Preview already exists; clear it before rebuilding")
            cursor = connection.execute(
                "SELECT * FROM jobs WHERE video_id=? AND kind='preview' AND snapshot=? "
                "AND status IN ('queued','running') AND deleting=0 ORDER BY created_at LIMIT 1",
                (video_id, snapshot),
            )
            row = cursor.fetchone()
            if row is not None:
                return dict(zip((column[0] for column in cursor.description), row, strict=True))
            job_id = uuid4().hex
            connection.execute(
                "INSERT INTO jobs (id,kind,status,created_at,updated_at,video_id,snapshot) "
                "VALUES (?,'preview','queued',?,?,?,?)",
                (job_id, now, now, video_id, snapshot),
            )
            return self._record(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def previews(self, video_id: str) -> list[Record]:
        self.get_video(video_id)
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM media_previews WHERE video_id=? ORDER BY created_at,id",
                    (video_id,),
                )
            )

    def get_preview(self, preview_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT media_previews.* FROM media_previews JOIN videos "
                    "ON videos.id=media_previews.video_id WHERE media_previews.id=? "
                    "AND videos.deleting=0",
                    (preview_id,),
                )
            )

    def clear_previews(self, video_id: str) -> Record:
        """Remove only rebuildable previews; subtitles, chats, sources and exports stay."""
        if _VIDEO_ID.fullmatch(video_id) is None:
            raise ValueError("Invalid video ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute("SELECT * FROM videos WHERE id=? AND deleting=0", (video_id,))
            )
            cancelled = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE video_id=? AND kind='preview' "
                    "AND status IN ('queued','running') ORDER BY created_at,id",
                    (video_id,),
                )
            ]
            connection.execute(
                "UPDATE jobs SET status='cancelled', updated_at=? WHERE video_id=? "
                "AND kind='preview' AND status IN ('queued','running')",
                (datetime.now(UTC).isoformat(), video_id),
            )
            connection.execute("DELETE FROM media_previews WHERE video_id=?", (video_id,))
        removed = 0
        # Sweep every published preview file name, including leftovers of an earlier failure;
        # in-progress temporary directories belong to their (now cancelled) job.
        with self._directory(Path("videos") / video_id / "previews", create=True) as directory:
            for name in os.listdir(directory):
                if _PREVIEW_FILE.fullmatch(name) is None:
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    continue
                os.unlink(name, dir_fd=directory)
                removed += 1
        return {"removed": removed, "cancelled_jobs": cancelled}

    def _tree_size(self, parent: int, name: str) -> int:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            return info.st_size
        if not stat.S_ISDIR(info.st_mode):
            return 0
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            return sum(self._tree_size(child, entry) for entry in os.listdir(child))
        finally:
            os.close(child)

    def deletion_scope(self, video_id: str) -> Record:
        """Counts shown before deletion; the confirmation token binds to this exact scope."""
        if _VIDEO_ID.fullmatch(video_id) is None:
            raise ValueError("Invalid video ID")
        with self._connect() as connection:
            video = self._record(
                connection.execute("SELECT * FROM videos WHERE id=? AND deleting=0", (video_id,))
            )
            counts: Record = {}
            for key, query in (
                ("assets", "SELECT COUNT(*) FROM media_assets WHERE video_id=?"),
                ("previews", "SELECT COUNT(*) FROM media_previews WHERE video_id=?"),
                ("subtitle_versions", "SELECT COUNT(*) FROM subtitle_versions WHERE video_id=?"),
                (
                    "conversations",
                    "SELECT COUNT(*) FROM conversations WHERE video_id=? AND status='active'",
                ),
                ("exports", "SELECT COUNT(*) FROM export_artifacts WHERE video_id=?"),
                ("flows", "SELECT COUNT(*) FROM flows WHERE video_id=?"),
                (
                    "active_jobs",
                    "SELECT COUNT(*) FROM jobs WHERE video_id=? AND status IN ('queued','running')",
                ),
            ):
                counts[key] = int(connection.execute(query, (video_id,)).fetchone()[0])
        size = 0
        with self._directory(Path("videos")) as videos:
            try:
                size = self._tree_size(videos, video_id)
            except FileNotFoundError:
                size = 0
        token = json.dumps({"video_id": video_id, **counts}, sort_keys=True)
        return {
            "video_id": video_id,
            "title": video["title"],
            **counts,
            "size_bytes": size,
            "confirmation": hashlib.sha256(token.encode()).hexdigest(),
        }

    def pending_deletions(self) -> list[Record]:
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT id,title,youtube_id FROM videos WHERE deleting=1 ORDER BY created_at,id"
                )
            )

    def _remove_entry(self, parent: int, name: str) -> None:
        """Unlink without following links, so nothing outside the managed tree is touched."""
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                for entry in os.listdir(child):
                    self._remove_entry(child, entry)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)

    def purge_video(self, video_id: str) -> None:
        """Second deletion phase: managed files first, then every owned row.

        A failure leaves the video marked deleting (pending cleanup) and is safe to retry.
        """
        if _VIDEO_ID.fullmatch(video_id) is None:
            raise ValueError("Invalid video ID")
        with self._connect() as connection:
            self._record(
                connection.execute("SELECT id FROM videos WHERE id=? AND deleting=1", (video_id,))
            )
        with self._directory(Path("videos")) as videos:
            try:
                self._remove_entry(videos, video_id)
            except FileNotFoundError:
                pass
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute("SELECT id FROM videos WHERE id=? AND deleting=1", (video_id,))
            )
            owned_jobs = "(SELECT id FROM jobs WHERE video_id=?)"
            for statement in (
                "DELETE FROM qa_messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE video_id=?)",
                "DELETE FROM conversations WHERE video_id=?",
                "DELETE FROM subtitle_cues WHERE version_id IN "
                "(SELECT id FROM subtitle_versions WHERE video_id=?)",
                "UPDATE subtitle_versions SET parent_id=NULL WHERE video_id=?",
                "DELETE FROM subtitle_versions WHERE video_id=?",
                "DELETE FROM export_artifacts WHERE video_id=?",
                "DELETE FROM media_previews WHERE video_id=?",
                f"DELETE FROM job_stages WHERE job_id IN {owned_jobs}",
                "DELETE FROM media_assets WHERE video_id=?",
                "DELETE FROM jobs WHERE video_id=?",
                "DELETE FROM flows WHERE video_id=?",
                "DELETE FROM videos WHERE id=?",
            ):
                connection.execute(statement, (video_id,))

    def create_flow(self, video_id: str, stage: str, snapshot: Record) -> Record:
        """Freeze the confirm-screen choices so later UI edits cannot affect this flow."""
        if stage not in FLOW_STAGES:
            raise ValueError("Invalid flow stage")
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        if scrub_text(encoded) != encoded:
            raise ValueError("Flow snapshot contains sensitive data")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection.execute(
                    "SELECT * FROM videos WHERE id = ? AND deleting = 0", (video_id,)
                )
            )
            if connection.execute(
                "SELECT 1 FROM flows WHERE video_id = ? AND status = 'running'", (video_id,)
            ).fetchone():
                raise ValueError("Flow already in progress")
            flow_id = uuid4().hex
            connection.execute(
                "INSERT INTO flows (id,video_id,status,stage,snapshot,created_at,updated_at) "
                "VALUES (?,?,'running',?,?,?,?)",
                (flow_id, video_id, stage, encoded, now, now),
            )
            return self._record(connection.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)))

    def get_flow(self, flow_id: str) -> Record:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT flows.* FROM flows JOIN videos ON videos.id=flows.video_id "
                    "WHERE flows.id = ? AND videos.deleting = 0",
                    (flow_id,),
                )
            )

    def flows(self, video_id: str) -> list[Record]:
        self.get_video(video_id)
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT * FROM flows WHERE video_id = ? ORDER BY created_at,id", (video_id,)
                )
            )

    def active_flow(self, video_id: str) -> Record | None:
        self.get_video(video_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT * FROM flows WHERE video_id = ? AND status = 'running' "
                "ORDER BY created_at,id LIMIT 1",
                (video_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip((column[0] for column in cursor.description), row, strict=True))

    def latest_flow(self, video_id: str) -> Record | None:
        """The newest flow in any status; the reloaded page uses it to label its last attempt."""
        self.get_video(video_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT * FROM flows WHERE video_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
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
            flow = self._record(connection.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)))
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

    def flow_jobs(self, flow_id: str) -> list[Record]:
        with self._connect() as connection:
            return self._records(
                connection.execute(
                    "SELECT jobs.* FROM jobs JOIN videos ON videos.id=jobs.video_id "
                    "WHERE jobs.flow_id = ? AND videos.deleting = 0 "
                    "AND jobs.deleting = 0 ORDER BY jobs.created_at, jobs.id",
                    (flow_id,),
                )
            )

    def set_job_flow(self, job_id: str, flow_id: str) -> None:
        """Attach a chained job to its flow so progress and cleanup stay grouped."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "UPDATE jobs SET flow_id = ?, updated_at = ? WHERE id = ? AND deleting = 0",
                    (flow_id, datetime.now(UTC).isoformat(), job_id),
                ).rowcount
                != 1
            ):
                raise ValueError("Job not found")

    def update_flow(
        self,
        flow_id: str,
        *,
        stage: str | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> Record:
        columns: dict[str, str | None] = {"updated_at": datetime.now(UTC).isoformat()}
        if stage is not None:
            if stage not in FLOW_STAGES:
                raise ValueError("Invalid flow stage")
            columns["stage"] = stage
        if status is not None:
            if status not in {"running", *FLOW_ENDED}:
                raise ValueError("Invalid flow status")
            columns["status"] = status
        columns["error_code"] = scrub_text(error) if error else None
        assignments = ", ".join(f"{column} = ?" for column in columns)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"UPDATE flows SET {assignments} WHERE id = ? AND status = 'running'",
                (*columns.values(), flow_id),
            ).rowcount
            if changed != 1 and status is None:
                raise ValueError("Flow is not running")
            return self._record(connection.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)))

    def interrupt_running(self) -> None:
        """Recovery never queues or resends an interrupted attempt."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'interrupted', updated_at = ? WHERE status = 'running' "
                "OR (kind='qa' AND status='queued')",
                (now,),
            )
            connection.execute(
                "UPDATE flows SET status = 'interrupted', updated_at = ? WHERE status = 'running'",
                (now,),
            )
            connection.execute(
                "UPDATE qa_messages SET status='failed',response_json=NULL,in_memory=0,"
                "memory_ids='[]',error_code='interrupted' WHERE status='pending'"
            )

    @contextmanager
    def _directory(self, relative: Path, *, create: bool = False) -> Iterator[int]:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Artifact path must stay inside the library")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root, flags)
        try:
            for component in relative.parts:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def video_dir(self, video_id: str) -> Path:
        """Map an application-generated ID to its managed artifact directories."""
        if _VIDEO_ID.fullmatch(video_id) is None:
            raise ValueError("Invalid video ID")
        relative = Path("videos") / video_id
        for name in _VIDEO_SUBDIRECTORIES:
            with self._directory(relative / name, create=True):
                pass
        return self.root / relative

    def publish(
        self,
        relative: Path,
        writer: Callable[[BinaryIO], object],
        validator: Callable[[BinaryIO], object],
    ) -> Path:
        """Write privately, validate fully, and atomically publish without replacement.

        Callbacks receive an open binary temporary file, pinned to the directory.
        Media subprocesses can inherit its descriptor and use /dev/fd/<descriptor>.
        This file helper does not replace the future job transaction/attempt gate.
        """
        if not relative.name or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Artifact path must stay inside the library")
        with self._directory(relative.parent) as directory:
            temporary_name = f".tmp-{uuid4().hex}"
            descriptor = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "w+b") as temporary:
                    writer(temporary)
                    temporary.flush()
                    temporary.seek(0)
                    validator(temporary)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    written = os.fstat(temporary.fileno())
                    current = os.stat(temporary_name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(current.st_mode) or current.st_ino != written.st_ino:
                        raise ValueError("Temporary artifact was replaced")
                os.link(
                    temporary_name,
                    relative.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            finally:
                os.unlink(temporary_name, dir_fd=directory)
        return self.root / relative
