"""Los archivos de preguntas de oro se validan en cada `make test`.

Hay uno por tema —`golden.yaml`, `golden-coches.yaml`, …— porque las preguntas
de un corpus de credenciales no miden nada sobre uno de fichas técnicas. Un
error de forma debe fallar aquí y no a mitad de la evaluación contra el
despliegue, que es cara y lenta.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

PRUEBAS = Path(__file__).resolve().parents[1]
ARCHIVOS = sorted(PRUEBAS.glob("golden*.y*ml"))


@pytest.fixture(params=ARCHIVOS, ids=lambda ruta: ruta.name)
def golden(request) -> dict:
    return yaml.safe_load(request.param.read_text(encoding="utf-8"))


def test_hay_al_menos_un_conjunto_de_oro():
    assert ARCHIVOS, "sin preguntas de oro no hay forma de medir una regresión"


def test_cada_conjunto_declara_su_tema():
    """Sin `perfil`, la evaluación mide el tema activo y no el que se pretendía."""
    for ruta in ARCHIVOS:
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        assert datos.get("perfil"), f"{ruta.name} no declara contra qué tema evalúa"


def test_el_archivo_tiene_version_y_preguntas(golden):
    assert golden["version"] == 1
    assert golden["preguntas"], "el conjunto de oro no puede estar vacío"


def test_los_identificadores_son_unicos(golden):
    ids = [caso["id"] for caso in golden["preguntas"]]

    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("campo", ["id", "pregunta", "tipo"])
def test_todos_los_casos_traen_los_campos_obligatorios(golden, campo):
    for caso in golden["preguntas"]:
        assert caso.get(campo), f"falta '{campo}' en {caso}"


def test_cada_tipo_trae_lo_que_le_corresponde(golden):
    for caso in golden["preguntas"]:
        if caso["tipo"] == "positiva":
            assert caso.get("documento_esperado"), f"{caso['id']} no declara documento esperado"
        elif caso["tipo"] == "negativa":
            assert caso.get("debe_declinar") is True, f"{caso['id']} negativa sin debe_declinar"
        else:
            pytest.fail(f"tipo desconocido en {caso['id']}: {caso['tipo']}")


def test_hay_negativas_suficientes(golden):
    """Entre 5 y 7 negativas sobre 20 preguntas (plan E7). Con menos, el reporte
    mide comodidad y no veracidad."""
    total = len(golden["preguntas"])
    negativas = sum(1 for c in golden["preguntas"] if c["tipo"] == "negativa")

    if total >= 20:
        assert 5 <= negativas <= 9, f"{negativas} negativas sobre {total} preguntas"
    else:
        assert negativas >= 1, "el conjunto de arranque necesita al menos una negativa"
