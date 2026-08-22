from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from luis_cv.infrastructure.config import Settings
from luis_cv.infrastructure.container import build_container, build_language_model
from luis_cv.infrastructure.inbound.http.app import create_app
from luis_cv.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from luis_cv.infrastructure.outbound.local.grounded_stub_model import GroundedStubLanguageModel

from .support.fakes import FrozenClock, RecordingTelemetry, SequentialIds

TOKEN = "token-de-prueba"
CORPUS = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def settings() -> Settings:
    """Adaptadores locales por defecto; con `LUISCV_INFERENCE_BACKEND=bedrock`
    la misma suite corre contra el modelo real (plan E1, nota de las dos
    familias)."""
    return Settings(
        api_token=TOKEN,
        retrieval_backend="local",
        inference_backend=os.getenv("LUISCV_INFERENCE_BACKEND", "stub"),
        aws_profile=os.getenv("LUISCV_AWS_PROFILE"),
        aws_region=os.getenv("LUISCV_AWS_REGION", "us-east-1"),
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
    modelo = (
        build_language_model(settings)
        if settings.uses_bedrock_inference
        else GroundedStubLanguageModel()
    )
    return build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=modelo,
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

    saltos = {
        "requires_bedrock": (
            os.getenv("LUISCV_INFERENCE_BACKEND") == "bedrock",
            "exige un modelo real; correr con LUISCV_INFERENCE_BACKEND=bedrock",
        ),
        "requires_kb": (
            os.getenv("LUISCV_RETRIEVAL_BACKEND") == "bedrock",
            "exige recuperación semántica; correr con LUISCV_RETRIEVAL_BACKEND=bedrock",
        ),
    }
    for marca, (disponible, motivo) in saltos.items():
        if disponible:
            continue
        salto = pytest.mark.skip(reason=motivo)
        for item in items:
            if marca in item.keywords:
                item.add_marker(salto)
