"""Preparación del corpus: las reglas que impiden un error irreversible.

El script vive en `scripts/`, fuera del paquete, porque es herramienta y no
servicio. Se prueba igual: sus dos reglas —vetar documentos de identidad y no
escribir datos dentro del repositorio— son las que, si fallan, no se arreglan
con un `rm`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prep_corpus", RAIZ / "scripts" / "prep_corpus.py")
prep = importlib.util.module_from_spec(_spec)
# El módulo debe estar registrado antes de ejecutarlo: `@dataclass` resuelve
# anotaciones mirando `sys.modules[cls.__module__]`.
sys.modules["prep_corpus"] = prep
_spec.loader.exec_module(prep)


@pytest.mark.parametrize(
    "texto",
    [
        "INSTITUTO NACIONAL ELECTORAL — CREDENCIAL PARA VOTAR",
        "CLAVE DE ELECTOR AAAABB000000H000",
        "pasaporte mexicano número X",
        "Licencia de conducir tipo A",
    ],
)
def test_los_documentos_de_identidad_quedan_vetados(texto):
    assert prep._vetado(texto) is not None


def test_un_cliente_llamado_instituto_nacional_electoral_no_activa_el_veto():
    """El veto mira el contenido del documento, no menciones del CV.

    Un currículum que enumera al INE como cliente es material legítimo; vetarlo
    dejaría fuera media trayectoria por una coincidencia de nombre.
    """
    cv = (
        "Participación en proyectos por convenio: Instituto Nacional Electoral — "
        "pruebas funcionales a los sistemas informáticos del proceso electoral."
    )

    assert prep._vetado(cv) is None


@pytest.mark.parametrize(
    "stem,esperado",
    [
        ("CV-2026", ("cv", 2026)),
        ("CV-2023", ("cv", 2023)),
        ("CertificadoSecundaria", ("certificado", None)),
        ("titulo-ingenieria-2021", ("titulo", 2021)),
        ("algo-suelto", ("documento", None)),
    ],
)
def test_el_tipo_y_el_anio_se_infieren_del_nombre(stem, esperado):
    assert prep._clasificar(stem) == esperado


def test_el_slug_produce_un_document_id_legible_y_sin_acentos():
    assert prep.slug("Maestría en Ingeniería en Seguridad") == "maestria-en-ingenieria-en-seguridad"


def test_las_transcripciones_manuales_leen_sus_metadatos(tmp_path):
    (tmp_path / "titulo.md").write_text("# Título\n\nContenido.", encoding="utf-8")
    (tmp_path / "titulo.md.metadata.json").write_text(
        '{"metadataAttributes": {"tipo": "titulo", "anio": 2021}}', encoding="utf-8"
    )

    (doc,) = prep.transcripciones(tmp_path)

    assert doc.nombre == "titulo.md"
    assert doc.metadata["tipo"] == "titulo"
    assert doc.metadata["anio"] == 2021


def test_sin_carpeta_de_transcripciones_no_falla(tmp_path):
    assert prep.transcripciones(tmp_path / "no-existe") == []
