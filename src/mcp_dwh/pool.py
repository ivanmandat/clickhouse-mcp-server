"""Пулы соединений.

Нужны из-за перехода на HTTP-транспорт: при stdio сервер обслуживал одного
пользователя, и общее соединение работало. По HTTP запросы идут параллельно, а
клиент clickhouse-connect на сессию допускает ровно один запрос за раз —
без пула 59 из 60 параллельных вызовов падают с
«Attempt to execute concurrent queries within the same session».
"""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator


class ClientPool:
    """Ленивый пул клиентов с ограничением сверху.

    Клиенты создаются по мере надобности и переиспользуются. Взятый клиент
    гарантированно не используется никем другим, пока не вернётся в пул.
    """

    def __init__(self, factory: Callable[[], Any], size: int = 8) -> None:
        self._factory = factory
        self._size = size
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self, timeout: float = 30.0) -> Iterator[Any]:
        client = self._take(timeout)
        broken = False
        try:
            yield client
        except Exception:
            # Соединение могло остаться в неконсистентном состоянии —
            # безопаснее выбросить его, чем вернуть в пул.
            broken = True
            raise
        finally:
            if broken:
                self._discard(client)
            else:
                self._idle.put(client)

    def _take(self, timeout: float) -> Any:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass

        with self._lock:
            if self._created < self._size:
                self._created += 1
                try:
                    return self._factory()
                except Exception:
                    self._created -= 1
                    raise

        # Пул исчерпан — ждём освобождения
        try:
            return self._idle.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"Все {self._size} соединений заняты дольше {timeout} с"
            ) from None

    def _discard(self, client: Any) -> None:
        with self._lock:
            self._created -= 1
        try:
            client.close()
        except Exception:  # noqa: BLE001 — закрываем на всякий случай
            pass

    def close(self) -> None:
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._created = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"created": self._created, "idle": self._idle.qsize(), "size": self._size}
