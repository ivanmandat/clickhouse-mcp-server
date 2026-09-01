"""HTTP-транспорт для развёртывания на сервере заказчика.

При stdio сервер запускал сам клиент, и вопрос доступа не стоял: процесс жил
в сессии пользователя. По HTTP сервер общий, доступен по сети и умеет читать
хранилище — поэтому без аутентификации его выставлять нельзя.

Проверка токена сделана ASGI-обёрткой вокруг приложения MCP, а не middleware
фреймворка: так она гарантированно выполняется раньше любой маршрутизации.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any, Awaitable, Callable

ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]

# Пути, доступные без токена. /health нужен оркестратору и healthcheck-у
# контейнера — иначе пришлось бы раздавать им секрет.
PUBLIC_PATHS = frozenset({"/health", "/healthz"})


async def _send_json(send: Callable, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuth:
    """Проверка Bearer-токена перед передачей запроса дальше."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if scope.get("path", "") in PUBLIC_PATHS:
            await self._app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        supplied = headers.get("authorization", "")

        # compare_digest, а не ==: сравнение по времени не должно подсказывать,
        # насколько токен близок к верному
        if not secrets.compare_digest(supplied, self._expected):
            await _send_json(
                send,
                401,
                {
                    "error": "unauthorized",
                    "detail": "Требуется заголовок Authorization: Bearer <токен>",
                },
            )
            return

        await self._app(scope, receive, send)


class HealthEndpoint:
    """Отвечает на /health без обращения к базам.

    Готовность сервера и доступность хранилища — разные вещи: контейнер должен
    считаться живым, даже когда ClickHouse временно недоступен, иначе
    оркестратор начнёт перезапускать здоровый процесс.
    """

    def __init__(self, app: ASGIApp, version: str) -> None:
        self._app = app
        self._version = version

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http" and scope.get("path", "") in PUBLIC_PATHS:
            await _send_json(send, 200, {"status": "ok", "version": self._version})
            return
        await self._app(scope, receive, send)


def build_app(mcp_server: Any, token: str | None, version: str) -> ASGIApp:
    app: ASGIApp = mcp_server.streamable_http_app()
    if token:
        app = BearerAuth(app, token)
    app = HealthEndpoint(app, version)
    return app


def serve(mcp_server: Any, version: str) -> None:
    import uvicorn

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8765"))
    from .config import _secret

    token = (_secret("MCP_AUTH_TOKEN") or "").strip()

    if not token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN не задан. HTTP-сервер даёт доступ к хранилищу, "
            "поднимать его без токена нельзя. Сгенерируйте: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    uvicorn.run(
        build_app(mcp_server, token, version),
        host=host,
        port=port,
        log_level=os.getenv("MCP_LOG_LEVEL", "info"),
        access_log=False,
    )
