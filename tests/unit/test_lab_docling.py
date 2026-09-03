"""El extractor de Docling se enchufa donde el de pypdf, sin que el pipeline note nada.

No se prueba a Docling —eso es lo que el banco de pruebas mide a mano, con
documentos reales— sino el contrato: que `instalar()` deja el registro en un
estado que `preparar` sabe recorrer, y que el `Documento` que sale trae las
mismas claves que el camino de producción. Si esa parte se rompe, la comparación
de corpus mide dos cosas distintas y no dice nada.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rag_agent.domain.profile import Profile
from rag_agent.infrastructure.ingest import extractors, preparar

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "scripts"))

from lab import docling_extractor  # noqa: E402

PUBLICO = Profile(slug="publico", name="Público", subject="folletos")


class _DocumentoFalso:
    """Lo justo de `docling.DoclingDocument` que el extractor toca."""

    def __init__(self, markdown: str, paginas: int, tablas: int):
        self._markdown = markdown
        self.pages = {i: object() for i in range(paginas)}
        self.tables = [object()] * tablas

    def export_to_markdown(self) -> str:
        return self._markdown


class _ResultadoFalso:
    def __init__(self, documento):
        self.document = documento
        self.status = "SUCCESS"


class _ConvertidorFalso:
    def __init__(self, markdown: str, paginas: int = 3, tablas: int = 2):
        self._markdown, self._paginas, self._tablas = markdown, paginas, tablas

    def convert(self, ruta):
        return _ResultadoFalso(_DocumentoFalso(self._markdown, self._paginas, self._tablas))


@pytest.fixture(autouse=True)
def registro_limpio(monkeypatch):
    """Ni el registro de extractores ni las mediciones sobreviven al test."""
    monkeypatch.setitem(extractors.POR_EXTENSION, ".pdf", extractors.POR_EXTENSION[".pdf"])
    docling_extractor.MEDICIONES.clear()
    yield
    docling_extractor.MEDICIONES.clear()
    docling_extractor._CONVERTIDORES.clear()


def _con_convertidor(monkeypatch, markdown, **kwargs):
    monkeypatch.setattr(
        docling_extractor, "_convertidor", lambda do_ocr: _ConvertidorFalso(markdown, **kwargs)
    )


TABLA = """## Versiones

| Versión | Precio |
|---|---|
| GLS | 450000 |
| Limited | 620000 |
""" + "Texto de relleno para pasar el mínimo. " * 10


def test_el_documento_trae_las_mismas_claves_que_el_extractor_de_produccion(monkeypatch, tmp_path):
    _con_convertidor(monkeypatch, TABLA)
    pdf = tmp_path / "hilux-2024.pdf"
    pdf.write_bytes(b"%PDF-1.4 da igual, el convertidor es falso")

    documento = docling_extractor.pdf_docling(pdf)

    assert documento.nombre == "hilux-2024.md"
    assert documento.texto.startswith("# hilux-2024\n\n")
    assert "| GLS | 450000 |" in documento.texto
    # Las claves que la recuperación y el manifiesto dan por hechas.
    assert documento.metadata["tipo"] == "documento"
    assert documento.metadata["fuente"] == "hilux-2024.pdf"
    assert documento.metadata["paginas"] == 3
    assert documento.metadata["anio"] == 2024
    assert documento.metadata["origen_texto"] == "docling"
    assert documento.metadata["docling_tablas"] == 2


def test_con_ocr_interno_el_origen_del_texto_lo_dice(monkeypatch, tmp_path):
    """Una cita que salió de un OCR no vale lo mismo que una del original."""
    _con_convertidor(monkeypatch, TABLA)
    pdf = tmp_path / "ficha.pdf"
    pdf.write_bytes(b"%PDF")

    documento = docling_extractor.pdf_docling(pdf, do_ocr=True)

    assert documento.metadata["origen_texto"] == "docling:ocr"


def test_un_pdf_sin_texto_util_se_descarta_igual_que_en_produccion(monkeypatch, tmp_path):
    _con_convertidor(monkeypatch, "dos palabras")
    pdf = tmp_path / "escaneo.pdf"
    pdf.write_bytes(b"%PDF")

    assert docling_extractor.pdf_docling(pdf) is None
    # Y aun así queda medido: lo que no produjo texto también es un resultado.
    medida = docling_extractor.MEDICIONES[str(pdf)]
    assert medida["caracteres"] == len("dos palabras")
    assert medida["archivo"] == "escaneo.pdf"


def test_el_veto_del_perfil_sigue_mandando(monkeypatch, tmp_path):
    """Docling no es una puerta trasera al corpus: el veto se aplica igual."""
    from rag_agent.infrastructure.ingest.documents import VetadoError

    _con_convertidor(monkeypatch, "CREDENCIAL PARA VOTAR " + "relleno " * 60)
    pdf = tmp_path / "ine.pdf"
    pdf.write_bytes(b"%PDF")

    with pytest.raises(VetadoError):
        docling_extractor.pdf_docling(pdf, banned=("CREDENCIAL PARA VOTAR",))


def test_instalar_deja_el_pipeline_recorriendo_el_corpus_con_docling(monkeypatch, tmp_path):
    """La prueba del enchufe: `preparar` no sabe que cambió el extractor."""
    _con_convertidor(monkeypatch, TABLA)
    origen = tmp_path / "originales"
    origen.mkdir()
    (origen / "mazda2-2026.pdf").write_bytes(b"%PDF")

    docling_extractor.instalar(do_ocr=False)
    reporte = preparar(origen, PUBLICO, carpeta_cache=tmp_path / ".cache", ocr=False)

    assert reporte.documentos == 1
    assert reporte.fragmentos
    assert reporte.fragmentos[0].metadata["origen_texto"] == "docling"
    assert reporte.fragmentos[0].metadata["fuente"] == "mazda2-2026.pdf"
    # `clase` la pone el pipeline, no el extractor: sigue viajando.
    assert "clase" in reporte.fragmentos[0].metadata


def test_dos_fichas_con_el_mismo_nombre_se_miden_por_separado(monkeypatch, tmp_path):
    """Indexar por nombre colapsaba las homónimas y escondía un documento."""
    _con_convertidor(monkeypatch, TABLA)
    for marca in ("marca-a", "marca-b"):
        (tmp_path / marca).mkdir()
        (tmp_path / marca / "ficha.pdf").write_bytes(b"%PDF")
        docling_extractor.pdf_docling(tmp_path / marca / "ficha.pdf")

    assert len(docling_extractor.MEDICIONES) == 2
    assert {m["archivo"] for m in docling_extractor.MEDICIONES.values()} == {"ficha.pdf"}
