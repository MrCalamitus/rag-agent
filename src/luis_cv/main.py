"""Punto de entrada del contenedor: `uvicorn luis_cv.main:app`."""

from __future__ import annotations

from .infrastructure.inbound.http.app import create_app

app = create_app()
