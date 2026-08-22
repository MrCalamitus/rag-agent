from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from luis_cv.infrastructure.config import Settings
from luis_cv.infrastructure.container import build_container
from luis_cv.infrastructure.inbound.http.app import create_app
from luis_cv.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from luis_cv.infrastructure.outbound.local.grounded_stub_model import GroundedStubLanguageModel

from .support.fakes import FrozenClock, RecordingTelemetry, SequentialIds

TOKEN = "token-de-prueba"
CORPUS = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_token=TOKEN,
        retrieval_backend="local",
        inference_backend="stub",
        corpus_dir=str(CORPUS),
        rate_limit_per_minute=20,
        _env_file=None,
    )


@pytest.fixture
def telemetry() -> RecordingTelemetry:
    return RecordingTelemetry()


@pytest.fixture
def container(settings: Settings, telemetry: RecordingTelemetry):
    """Contenedor con corpus de prueba: sin AWS, sin red, determinista."""
    return build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=GroundedStubLanguageModel(),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )


@pytest.fixture
def app(container):
    return create_app(container)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def build_client(container) -> TestClient:
    return TestClient(create_app(container), raise_server_exceptions=False)


def pytest_collection_modifyitems(config, items):
    """Los casos marcados `requires_bedrock` solo corren contra un modelo real.

    Se saltan de forma explícita —con motivo visible en el reporte— en vez de
    borrarse: forman parte de la suite de aceptación y deben pasar contra el
    despliegue (`LUISCV_INFERENCE_BACKEND=bedrock make test`).
    """
    import os

    if os.getenv("LUISCV_INFERENCE_BACKEND") == "bedrock":
        return
    motivo = pytest.mark.skip(
        reason="exige un modelo real; correr con LUISCV_INFERENCE_BACKEND=bedrock"
    )
    for item in items:
        if "requires_bedrock" in item.keywords:
            item.add_marker(motivo)
