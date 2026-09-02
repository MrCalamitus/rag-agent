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
