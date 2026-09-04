"""Punto de entrada del contenedor: `uvicorn rag_agent.main:app`."""

from __future__ import annotations

from .infrastructure.inbound.http.app import create_app

app = create_app()
