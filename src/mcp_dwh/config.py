"""Конфигурация.

Единственный источник — переменные окружения. При локальной разработке они
подхватываются из .env рядом с проектом, в контейнере передаются compose-ом
или оркестратором; файла .env там нет и быть не должно.

Путь к .env вычисляется от расположения пакета, но ТОЛЬКО когда пакет лежит
в дереве исходников. После установки в site-packages такой расчёт даёт
каталог интерпретатора — из-за этого отчёты уходили мимо смонтированного тома.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PKG_DIR = Path(__file__).resolve().parent


def _find_project_root() -> Path | None:
    """Корень репозитория, если пакет запущен из исходников."""
    candidate = _PKG_DIR.parents[1]  # src/mcp_dwh -> src -> корень
    if (candidate / "pyproject.toml").exists():
        return candidate
    return None


PROJECT_ROOT = _find_project_root()
if PROJECT_ROOT is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _secret(name: str) -> str | None:
    """Значение секрета: сначала из файла, потом из переменной.

    Соглашение <ИМЯ>_FILE то же, что у официальных образов ClickHouse и
    Postgres. Файл предпочтительнее переменной: содержимое env видно в
    `docker inspect` и попадает в логи оркестратора, а смонтированный секрет —
    нет. Совместимость с переменной сохранена, чтобы не ломать stdio-режим
    и локальную разработку.
    """
    path = os.getenv(name + "_FILE")
    if path:
        try:
            # rstrip: редактор мог дописать перевод строки в конец файла
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось прочитать секрет {name} из файла {path}: {exc}"
            ) from None
    return os.getenv(name)


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or default


@dataclass(frozen=True)
class ClickHouseConfig:
    host: str
    port: int
    user: str
    password: str

    # TLS обязателен, когда база на другом сервере: пароль и данные иначе
    # идут по сети открытым текстом. Порт по умолчанию тогда 8443.
    secure: bool = False
    verify: bool = True

    # Слои, разрешённые к использованию. Список нужен не для безопасности —
    # её обеспечивают гранты на стороне ClickHouse, — а чтобы интроспекция
    # не показывала агенту лишнего. У заказчика слои могут называться иначе,
    # поэтому список настраивается, а не зашит в код.
    allowed_databases: tuple[str, ...] = ("core", "mart")

    # Потолок для интерактивных запросов. Серверные лимиты строже и главнее,
    # этот нужен, чтобы не тащить в контекст агента простыню строк.
    default_limit: int = 200
    max_limit: int = 5000

    pool_size: int = 8


@dataclass(frozen=True)
class PostgresConfig:
    dsn: str
    pool_min: int = 1
    pool_max: int = 8


@dataclass(frozen=True)
class GrafanaConfig:
    url: str
    user: str
    password: str
    datasource_uid: str


@dataclass(frozen=True)
class Config:
    clickhouse: ClickHouseConfig
    postgres: PostgresConfig
    grafana: GrafanaConfig
    reports_dir: Path = field(default_factory=Path)

    def redacted(self) -> dict:
        """Снимок настроек без секретов — для диагностики."""
        ch = self.clickhouse
        dsn = self.postgres.dsn
        # прячем пароль внутри DSN, оставляя остальное читаемым
        if "@" in dsn and "//" in dsn:
            head, _, tail = dsn.partition("//")
            creds, _, hostpart = tail.partition("@")
            user = creds.split(":")[0] if ":" in creds else creds
            dsn = f"{head}//{user}:***@{hostpart}"
        return {
            "clickhouse": {
                "host": ch.host,
                "port": ch.port,
                "user": ch.user,
                "secure": ch.secure,
                "allowed_databases": list(ch.allowed_databases),
                "default_limit": ch.default_limit,
                "max_limit": ch.max_limit,
                "pool_size": ch.pool_size,
            },
            "postgres": {"dsn": dsn, "pool_max": self.postgres.pool_max},
            "grafana": {"url": self.grafana.url, "datasource_uid": self.grafana.datasource_uid},
            "reports_dir": str(self.reports_dir),
        }


def _require(name: str) -> str:
    value = _secret(name)
    if not value:
        hint = f" Проверьте {PROJECT_ROOT / '.env'}" if PROJECT_ROOT else ""
        raise RuntimeError(
            f"Не задана переменная {name} (или {name}_FILE).{hint}"
        )
    return value


def _default_reports_dir() -> Path:
    if PROJECT_ROOT is not None:
        return PROJECT_ROOT / "reports"
    # В контейнере пакет установлен, корня исходников нет: путь задаётся явно
    return Path("/app/reports")


def load_config() -> Config:
    secure = _flag("CH_SECURE")
    default_port = 8443 if secure else 8123

    return Config(
        clickhouse=ClickHouseConfig(
            host=os.getenv("CH_HOST", "127.0.0.1"),
            port=int(os.getenv("CH_HTTP_PORT", str(default_port))),
            user=os.getenv("CH_DASHBOARD_USER", "dashboard"),
            password=_require("CH_DASHBOARD_PASSWORD"),
            secure=secure,
            verify=_flag("CH_VERIFY", True),
            allowed_databases=_csv("CH_ALLOWED_DATABASES", ("core", "mart")),
            default_limit=int(os.getenv("CH_DEFAULT_LIMIT", "200")),
            max_limit=int(os.getenv("CH_MAX_LIMIT", "5000")),
            pool_size=int(os.getenv("CH_POOL_SIZE", "8")),
        ),
        postgres=PostgresConfig(
            dsn=_require("PG_DSN"),
            pool_min=int(os.getenv("PG_POOL_MIN", "1")),
            pool_max=int(os.getenv("PG_POOL_MAX", "8")),
        ),
        grafana=GrafanaConfig(
            url=os.getenv("GF_URL", "http://127.0.0.1:3000"),
            user=os.getenv("GF_ADMIN_USER", "admin"),
            password=_secret("GF_ADMIN_PASSWORD") or "",
            datasource_uid=os.getenv("GF_DATASOURCE_UID", "clickhouse-dwh"),
        ),
        reports_dir=Path(os.getenv("MCP_REPORTS_DIR", str(_default_reports_dir()))),
    )
