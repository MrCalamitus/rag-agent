"""Troceado: qué es un fragmento citable.

El troceo es lo que hace posible un corpus de folletos largos sin romper el
contrato de citación. Estas pruebas fijan las tres propiedades de las que
depende el resto: se respeta el máximo, se solapa, y un documento corto sigue
saliendo entero con el `document_id` de siempre.
"""

from __future__ import annotations

import pytest

from rag_agent.domain.chunking import chunk_document_id, split
from rag_agent.domain.profile import ChunkPolicy

POLITICA = ChunkPolicy(max_chars=600, overlap_chars=80, min_chars_to_split=800)


def _largo(secciones: int = 4, frases: int = 40) -> str:
    partes = []
    for i in range(secciones):
        partes.append(f"## Sección {i}\n\n" + " ".join(f"Dato número {i}-{j}." for j in range(frases)))
    return "\n\n".join(partes)


def test_un_documento_corto_sale_entero_y_conserva_su_id():
    """La estrategia del corpus de credenciales sobrevive como caso particular."""
    fragmentos = split("Título profesional expedido en 2019.", POLITICA)

    assert len(fragmentos) == 1
    assert fragmentos[0].is_whole_document
    assert chunk_document_id("titulo-2019", fragmentos[0]) == "titulo-2019"


def test_ningun_fragmento_excede_el_maximo():
    """El máximo es un límite real, no una orientación: el solape que se
    antepone al reabrir un fragmento tiene que caber dentro."""
    for fragmento in split(_largo(), POLITICA):
        assert len(fragmento.text) <= POLITICA.max_chars


def test_los_fragmentos_se_solapan():
    """El dato que cae en la costura debe aparecer entero en algún lado."""
    fragmentos = split(_largo(), POLITICA)

    assert len(fragmentos) > 1
    for previo, siguiente in zip(fragmentos, fragmentos[1:]):
        cola = previo.text[-POLITICA.overlap_chars :].strip()
        # Alguna parte del final del anterior reaparece al principio del siguiente.
        assert any(palabra in siguiente.text[:200] for palabra in cola.split() if len(palabra) > 3)


def test_no_se_pierde_contenido():
    texto = _largo()
    unidas = " ".join(f.text for f in split(texto, POLITICA))

    for marcador in ("Dato número 0-0.", "Dato número 3-39.", "Sección 2"):
        assert marcador in unidas


def test_los_ids_numeran_los_fragmentos_de_forma_estable():
    fragmentos = split(_largo(), POLITICA)
    ids = [chunk_document_id("hilux-2024", f) for f in fragmentos]

    assert ids[0] == "hilux-2024--001"
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids), "el orden lexicográfico debe seguir al del documento"


def test_un_texto_vacio_no_produce_fragmentos():
    assert split("   \n\n  ", POLITICA) == ()


@pytest.mark.parametrize("politica", [{"max_chars": 0}, {"overlap_chars": -1}, {"overlap_chars": 900}])
def test_una_politica_incoherente_no_puede_construirse(politica):
    """Un solape mayor que el fragmento es un bucle infinito esperando a pasar."""
    with pytest.raises(ValueError):
        ChunkPolicy(**{"max_chars": 600, "overlap_chars": 80, **politica})


TABLA = (
    "| VERSIÓN | I | I SPORT | SIGNATURE |\n"
    "|---|---|---|---|\n"
    + "\n".join(
        f"| Característica número {i} de la ficha | - | • | • |" for i in range(40)
    )
)


def test_una_tabla_partida_repite_su_cabecera_en_cada_fragmento():
    """Sin la cabecera, `| Espejo | - | • | • |` no dice de qué versión habla.

    Es la propiedad de la que depende que una ficha multi-versión sirva de algo:
    la fila sola es un renglón de viñetas sueltas, y el agente no puede
    responder si el equipamiento es de la I SPORT o de la SIGNATURE.
    """
    fragmentos = split(TABLA, POLITICA)

    assert len(fragmentos) > 1, "la tabla de prueba debe partirse para que el test diga algo"
    for fragmento in fragmentos:
        assert "| VERSIÓN | I | I SPORT | SIGNATURE |" in fragmento.text
        assert "|---|---|---|---|" in fragmento.text


def test_la_cabecera_repetida_cabe_dentro_del_maximo():
    """Se descuenta del presupuesto, no se añade encima."""
    for fragmento in split(TABLA, POLITICA):
        assert len(fragmento.text) <= POLITICA.max_chars


def test_las_filas_de_la_tabla_siguen_siendo_lineas():
    """Unir las filas con espacio aplastaba la tabla en un solo renglón."""
    fragmentos = split(TABLA, POLITICA)

    for fragmento in fragmentos:
        filas = [l for l in fragmento.text.splitlines() if l.startswith("| Característica")]
        assert filas, "cada fragmento debe traer filas de datos, no solo la cabecera"
        for fila in filas:
            assert fila.count("|") == 5, f"fila aplastada: {fila!r}"


def test_no_se_pierde_ninguna_fila_al_partir_la_tabla():
    unidas = "\n".join(f.text for f in split(TABLA, POLITICA))

    for i in range(40):
        assert f"| Característica número {i} de la ficha |" in unidas


def test_un_listado_que_empieza_por_barra_no_se_confunde_con_una_tabla():
    """La línea de guiones es la firma; sin ella no hay cabecera que repetir."""
    listado = "\n".join(f"| dato suelto número {i} sin tabla alrededor" for i in range(80))

    fragmentos = split(listado, POLITICA)

    assert len(fragmentos) > 1
    # La primera línea no se repite: no era una cabecera.
    assert sum(f.text.count("| dato suelto número 0 sin") for f in fragmentos) == 1


def test_el_solape_no_arrastra_filas_de_tabla_huerfanas():
    """Una fila sin cabecera al principio del fragmento es ruido, no contexto.

    El fragmento siguiente ya abre con su propia cabecera; colar delante las
    últimas filas del anterior devolvía justo el renglón anónimo que el troceo
    existe para evitar.
    """
    for fragmento in split(TABLA, POLITICA):
        lineas = [l for l in fragmento.text.splitlines() if l.strip()]
        assert lineas[0].startswith("| VERSIÓN"), f"empieza huérfano: {lineas[0]!r}"


ROTULADA = (
    "| MAZDA3 SEDÁN 2026 | MAZDA3 SEDÁN 2026 | MAZDA3 SEDÁN 2026 |\n"
    "|---|---|---|\n"
    "| VERSIÓN | I SPORT | SIGNATURE |\n"
    + "\n".join(f"| Característica número {i} de la ficha | • | • |" for i in range(40))
)


def test_un_rotulo_que_abarca_la_tabla_no_desplaza_a_los_nombres_de_columna():
    """Repetir solo «MAZDA3 SEDÁN 2026» diría el modelo, no la versión.

    Es la pregunta que se le hace al agente: no de qué coche habla la ficha
    —eso ya lo dice el `document_id`— sino cuál de sus versiones trae el equipo.
    """
    fragmentos = split(ROTULADA, POLITICA)

    assert len(fragmentos) > 1
    for fragmento in fragmentos:
        assert "| VERSIÓN | I SPORT | SIGNATURE |" in fragmento.text


def test_una_cabecera_normal_no_se_come_la_primera_fila_de_datos():
    """La regla solo aplica al rótulo repetido; si no, se repetiría un dato."""
    fragmentos = split(TABLA, POLITICA)

    primera = "| Característica número 0 de la ficha | - | • | • |"
    assert sum(f.text.count(primera) for f in fragmentos) == 1
