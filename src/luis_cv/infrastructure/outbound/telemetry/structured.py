"""Telemetría estructurada (plan E6).

Los logs llevan identificadores, contadores y latencias. Nunca el texto del
turno: para correlacionar una respuesta concreta se usa su huella
(`fingerprint`), que permite comparar sin poder reconstruir.

Los `span` se emiten como eventos con duración —`retrieval` e `inference` por
separado— que es la forma en que se enganchará X-Ray sin tocar el núcleo.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_LOGGER_NAME = "luis_cv"


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    return logger


class StructuredTelemetry:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or configure_logging()

    def event(self, name: str, /, **fields: object) -> None:
        self._emit(logging.INFO, name, fields)

    def warning(self, name: str, /, **fields: object) -> None:
        self._emit(logging.WARNING, name, fields)

    def error(self, name: str, /, **fields: object) -> None:
        self._emit(logging.ERROR, name, fields)

    @asynccontextmanager
    async def span(self, name: str, /, **fields: object) -> AsyncIterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self._emit(
                logging.INFO,
                f"span.{name}",
                {**fields, "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )

    def _emit(self, level: int, name: str, fields: dict[str, object]) -> None:
        payload = {"event": name, **{k: v for k, v in fields.items() if v is not None}}
        self._logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
