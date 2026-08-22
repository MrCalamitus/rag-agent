"""Servidor real en un hilo, para las pruebas donde el transporte importa.

El cliente de pruebas de Starlette entrega la respuesta ya completa: sirve para
verificar contenido, no para medir si los deltas salen espaciados. El caso B8
—sin buffering— solo significa algo sobre un socket de verdad, que es la misma
propiedad que se vuelve a verificar contra el ALB.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager

import uvicorn


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def running_app(app, *, timeout: float = 10.0) -> Iterator[str]:
    puerto = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=puerto, log_level="warning")
    server = uvicorn.Server(config)
    hilo = threading.Thread(target=server.run, daemon=True)
    hilo.start()

    limite = time.monotonic() + timeout
    while not server.started and time.monotonic() < limite:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - entorno degradado
        server.should_exit = True
        raise RuntimeError("El servidor de pruebas no arrancó a tiempo.")

    try:
        yield f"http://127.0.0.1:{puerto}"
    finally:
        server.should_exit = True
        hilo.join(timeout=timeout)
