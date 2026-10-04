"""Offline HTTP boundary tests for the local workspace."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from video_content_capture.cli import app as cli
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VCC_HOST",
        "VCC_PORT",
        "VCC_LIBRARY_DIR",
        "VCC_GEMINI_TRANSLATION_MODEL",
        "VCC_GEMINI_QA_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def client_for(project: Path) -> TestClient:
    return TestClient(create_app(load_settings(project)), base_url="http://127.0.0.1:8765")


def test_status_videos_and_page_never_contain_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "fake-api-sentinel-key"
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={secret}\n")
    with client_for(tmp_path) as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json() == {
            "version": "0.1.0",
            "gemini": {
                "configured": True,
                "translation_model": "gemini-3.5-flash-lite",
                "qa_model": "gemini-3.8-flash",
            },
        }
        assert client.get("/api/videos").json() == []
        for route in ("/", "/api/status", "/api/videos", "/missing", "/static/workspace.js"):
            response = client.get(route)
            assert secret not in response.text
            assert secret not in str(response.headers)
        logging.getLogger("vcc.workspace").warning("provider failure %s", secret)
    assert secret not in caplog.text
    for artifact in (tmp_path / "outputs/library").rglob("*"):
        if artifact.is_file():
            assert secret.encode() not in artifact.read_bytes()


def test_missing_key_status_and_semantic_empty_page(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/api/status").json()["gemini"]["configured"] is False
        html = client.get("/").text
        for text in (
            "影片庫",
            "播放與字幕",
            "影片問答",
            "GEMINI_API_KEY",
            "重新啟動",
            "製作相容預覽",
        ):
            assert text in html
        assert '<label for="youtube-url">' in html
        assert 'type="password"' not in html
        assert "disabled" in html
        assert client.get("/static/workspace.css").status_code == 200
        assert client.get("/static/workspace.js").status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:9999", "localhost.evil:8765"])
def test_invalid_host_rejected(tmp_path: Path, host: str) -> None:
    with client_for(tmp_path) as client:
        response = client.get("/api/status", headers={"Host": host})
        assert response.status_code == 400
        assert host not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:9999", None])
def test_unsafe_write_rejected_before_routing(
    tmp_path: Path, method: str, origin: str | None
) -> None:
    with client_for(tmp_path) as client:
        response = client.request(
            method, "/api/videos", headers={"Origin": origin} if origin else {}
        )
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers


def test_same_origin_request_and_no_cors(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post("/api/videos", headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 405  # S1 deliberately has no write route.
        response = client.get("/api/status", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        response = client.options(
            "/api/videos",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in response.headers


def test_serve_uses_explicit_options_and_disables_access_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        cli,
        [
            "serve",
            "--project-dir",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            "9003",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9003
    assert calls[0]["access_log"] is False
    assert calls[0]["proxy_headers"] is False


def test_serve_rejects_nonloopback_and_key_cli_option(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["serve", "--project-dir", str(tmp_path), "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "loopback" in result.output
    assert runner.invoke(cli, ["serve", "--gemini-api-key", "fake-key"]).exit_code != 0


def test_app_restart_preserves_library_and_interrupts_work(tmp_path: Path) -> None:
    import sqlite3
    from uuid import uuid4

    with client_for(tmp_path) as first:
        assert first.get("/api/videos").json() == []
        with sqlite3.connect(tmp_path / "outputs/library/library.sqlite3") as connection:
            connection.execute(
                "INSERT INTO videos (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (
                    uuid4().hex,
                    "離線影片",
                    "before",
                    "before",
                ),
            )
            connection.execute(
                "INSERT INTO jobs (id, kind, status, attempt_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    "download",
                    "running",
                    uuid4().hex,
                    "before",
                    "before",
                ),
            )
    with client_for(tmp_path) as second:
        assert second.get("/api/videos").json()[0]["title"] == "離線影片"
        with sqlite3.connect(tmp_path / "outputs/library/library.sqlite3") as connection:
            assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "interrupted"


def test_backend_errors_and_logs_are_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from video_content_capture.workspace.storage import Library

    secret = "fake-error-sentinel-key"
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={secret}\n")

    def failing_list(library: Library) -> list[dict[str, str]]:
        logging.getLogger("uvicorn.error").error("Error %s", secret)
        raise ValueError(secret)

    monkeypatch.setattr(Library, "list_videos", failing_list)
    with client_for(tmp_path) as client:
        response = client.get("/api/videos")
        assert response.status_code == 500
        assert secret not in response.text
        assert secret not in str(response.headers)
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_duplicate_host_and_fetch_metadata_are_rejected(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert (
            client.get(
                "/api/status",
                headers=[
                    ("Host", "127.0.0.1:8765"),
                    ("Host", "evil.example:8765"),
                ],
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/videos",
                headers={
                    "Origin": "http://127.0.0.1:8765",
                    "Sec-Fetch-Site": "cross-site",
                },
            ).status_code
            == 403
        )


def test_serve_localhost_binds_numeric_loopback_without_dns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    hosts: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: hosts.append(kwargs["host"]))
    result = CliRunner().invoke(
        cli, ["serve", "--project-dir", str(tmp_path), "--host", "localhost"]
    )
    assert result.exit_code == 0, result.output
    assert hosts == ["127.0.0.1"]
