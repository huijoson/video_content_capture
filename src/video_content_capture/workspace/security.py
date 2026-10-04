"""Local HTTP boundary: exact authorities, same-origin writes, redacted output."""

from __future__ import annotations

import json
import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from video_content_capture.redaction import RedactingFilter, register_secrets, scrub_text
from video_content_capture.workspace.config import WorkspaceSettings


def configure_redaction(settings: WorkspaceSettings) -> None:
    secrets = [settings.gemini_api_key.get_secret_value()] if settings.gemini_api_key else []
    register_secrets(secrets)
    for name in ("vcc.workspace", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, RedactingFilter) for item in logger.filters):
            logger.addFilter(RedactingFilter())
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, RedactingFilter) for item in handler.filters):
            handler.addFilter(RedactingFilter())


class LocalBoundary:
    def __init__(self, app: ASGIApp, settings: WorkspaceSettings) -> None:
        self.app = app
        self.settings = settings
        self.secrets = (
            [settings.gemini_api_key.get_secret_value()] if settings.gemini_api_key else []
        )
        self.authorities = {
            f"{host}:{settings.port}" for host in ("localhost", "127.0.0.1", "[::1]")
        }
        bound_host = f"[{settings.host}]" if ":" in settings.host else settings.host
        self.authorities.add(f"{bound_host}:{settings.port}")
        if settings.port == 80:
            self.authorities.update(("localhost", "127.0.0.1", "[::1]", bound_host))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or hosts[0].lower() not in self.authorities:
            await JSONResponse({"detail": "Invalid Host"}, status_code=400)(scope, receive, send)
            return
        origins = headers.getlist("origin")
        safe_origin = len(origins) == 1 and origins[0] == f"http://{hosts[0].lower()}"
        writing = scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if (origins and not safe_origin) or (
            writing and (not safe_origin or headers.get("sec-fetch-site") == "cross-site")
        ):
            await JSONResponse({"detail": "Same-origin request required"}, status_code=403)(
                scope, receive, send
            )
            return
        started = False
        textual = True

        async def safe_send(message: Message) -> None:
            nonlocal started, textual
            if message["type"] == "http.response.start":
                started = True
                content_type = Headers(raw=message.get("headers", [])).get("content-type", "")
                textual = content_type.startswith("text/") or content_type.startswith(
                    "application/json"
                )
                # Redaction applies to application text, never binary media or Range bytes.
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if not textual or name.lower() != b"content-length"
                ] + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; frame-ancestors 'none'; "
                        b"base-uri 'none'; form-action 'self'",
                    ),
                ]
            elif message["type"] == "http.response.body" and self.secrets and textual:
                body = message.get("body", b"")
                for secret in self.secrets:
                    for encoded in (secret.encode(), json.dumps(secret)[1:-1].encode()):
                        body = body.replace(encoded, b"[REDACTED]")
                message["body"] = body
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception:
            logging.getLogger("vcc.workspace").error("Workspace request failed")
            if not started:
                await JSONResponse({"detail": "Workspace request failed"}, status_code=500)(
                    scope, receive, safe_send
                )


def public_text(text: str, settings: WorkspaceSettings) -> str:
    secrets = [settings.gemini_api_key.get_secret_value()] if settings.gemini_api_key else []
    return scrub_text(text, secrets)
