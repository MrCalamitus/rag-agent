"""Asistente de configuración: escribe los archivos que mandan.

Genera `.env` y `infra/terraform.tfvars`, que son los que deciden dónde se
despliega el proyecto y con qué nombre. Un error aquí no rompe una prueba: crea
recursos en la cuenta equivocada o deja el servicio apuntando a un tema que no
es. Se prueba lo que se escribe y, sobre todo, lo que se conserva.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_agent.infrastructure.inbound.cli import console
from rag_agent.infrastructure.inbound.cli.init import (
    _ENV,
    _TFVAR,
    Proyecto,
    escribir_env,
    escribir_tfvars,
    leer_pares,
)

PROYECTO = Proyecto(
    nombre="rag-coches",
    entorno="prod",
    aws_profile="luis",
    aws_account="123456789012",
    aws_region="us-east-1",
    api_token="token-local",
    default_model="agente-rag-sonnet",
)


def test_el_env_generado_se_relee_y_fija_el_tema_activo(tmp_path):
    destino = tmp_path / ".env"

    escribir_env(destino, PROYECTO, "coches")

    valores = leer_pares(destino, _ENV)
    assert valores["RAG_DEFAULT_PROFILE"] == "coches"
    assert valores["RAG_API_TOKEN"] == "token-local"
    assert valores["RAG_PROFILES_DIR"] == "profiles"


def test_reconfigurar_conserva_los_backends_ya_elegidos(tmp_path):
    """Cambiar de región no debe devolver los backends a sus valores de fábrica:
    quien puso `bedrock` lo hizo a propósito."""
    destino = tmp_path / ".env"
    destino.write_text(
        "RAG_RETRIEVAL_BACKEND=bedrock\nRAG_INFERENCE_BACKEND=bedrock\nRAG_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )

    escribir_env(destino, PROYECTO, "coches")

    valores = leer_pares(destino, _ENV)
    assert valores["RAG_RETRIEVAL_BACKEND"] == "bedrock"
    assert valores["RAG_INFERENCE_BACKEND"] == "bedrock"
    assert valores["RAG_LOG_LEVEL"] == "DEBUG"


def test_el_tfvars_generado_declara_cuenta_proyecto_y_region(tmp_path):
    destino = tmp_path / "terraform.tfvars"

    escribir_tfvars(destino, PROYECTO)

    valores = leer_pares(destino, _TFVAR)
    assert valores["aws_account_id"] == "123456789012"
    assert valores["project"] == "rag-coches"
    assert valores["aws_region"] == "us-east-1"


def test_reconfigurar_no_pierde_el_certificado(tmp_path):
    """Perder el ARN de ACM devolvería el ALB a HTTP en claro sin avisar."""
    destino = tmp_path / "terraform.tfvars"
    destino.write_text('certificate_arn = "arn:aws:acm:us-east-1:1:certificate/x"\n', encoding="utf-8")

    escribir_tfvars(destino, PROYECTO)

    assert leer_pares(destino, _TFVAR)["certificate_arn"].startswith("arn:aws:acm:")


def test_los_comentarios_no_se_leen_como_valores(tmp_path):
    destino = tmp_path / ".env"
    destino.write_text("# RAG_API_TOKEN=no-usar\nRAG_API_TOKEN=si-usar\n", encoding="utf-8")

    assert leer_pares(destino, _ENV)["RAG_API_TOKEN"] == "si-usar"


def test_un_archivo_inexistente_no_rompe_la_lectura(tmp_path):
    assert leer_pares(tmp_path / "no-existe", _ENV) == {}


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("Marcas de Coches", "marcas-de-coches"),
        ("Inversión & Ahorro", "inversion-ahorro"),
        ("  RAG   Técnico  ", "rag-tecnico"),
    ],
)
def test_los_nombres_se_convierten_en_slugs_usables(entrada, esperado):
    """El slug acaba siendo nombre de recurso en AWS y de archivo: sin acentos,
    sin espacios y sin sorpresas."""
    assert console.slugificar(entrada) == esperado


def test_fijar_el_tema_activo_no_reescribe_el_resto_del_env(tmp_path, monkeypatch):
    from rag_agent.infrastructure.inbound.cli import menu

    env = tmp_path / ".env"
    env.write_text("RAG_API_TOKEN=x\nRAG_DEFAULT_PROFILE=viejo\nRAG_LOG_LEVEL=INFO\n", encoding="utf-8")
    monkeypatch.setattr(menu, "RAIZ", tmp_path)

    menu._fijar_perfil_activo("nuevo")

    valores = leer_pares(env, _ENV)
    assert valores == {"RAG_API_TOKEN": "x", "RAG_DEFAULT_PROFILE": "nuevo", "RAG_LOG_LEVEL": "INFO"}


def test_el_estado_se_puede_cargar_sin_configuracion(tmp_path, monkeypatch):
    """El menú tiene que arrancar en un repositorio recién clonado y decir qué
    falta, no reventar por no encontrar `.env`."""
    from rag_agent.infrastructure.inbound.cli import menu

    monkeypatch.setattr(menu, "RAIZ", tmp_path)

    estado = menu.cargar_estado()

    assert estado.proyecto is None
    assert estado.perfiles == {}
    assert not estado.inicializado
