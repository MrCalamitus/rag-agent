"""Extractor de PDF con Docling, para compararlo con el de pypdf.

El extractor actual (`ingest.extractors.pdf`) saca el texto con pypdf y
reconstruye a mano lo que el PDF no dice: los folios y los encabezados corridos
se deducen por posición y repetición en `extractors.limpiar`, y la estructura de
tabla solo se recupera pagando Textract.

Docling ataca las dos cosas con un modelo de layout: etiqueta encabezados y pies
como *furniture* y los deja fuera del Markdown, y pasa las tablas por TableFormer
para devolverlas ya formadas. Todo en local. Si eso se sostiene sobre este
corpus, sustituye a la vez al extractor, a `limpiar` y a la factura de OCR.

Este módulo existe para medirlo, no para adoptarlo: se enchufa en el registro de
extractores desde `scripts/prep_corpus_docling.py` y el pipeline no se entera.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rag_agent.domain.profile import CleanupPolicy
from rag_agent.infrastructure.ingest.documents import (
    Documento,
    VetadoError,
    anio_en,
    clasificar,
    marcador_vetado,
    slug,
)
from rag_agent.infrastructure.ingest.extractors import MINIMO_TEXTO

# Un convertidor por modo. Construirlo carga los modelos de layout y de tabla:
# hacerlo por documento convertiría un lote de cien en una tarde.
_CONVERTIDORES: dict[bool, object] = {}
# Lo que cada conversión costó, para el reporte del script. Se indexa por nombre
# de archivo porque es lo que el `Reporte` del pipeline conserva.
MEDICIONES: dict[str, dict] = {}


def _convertidor(do_ocr: bool):
    if do_ocr in _CONVERTIDORES:
        return _CONVERTIDORES[do_ocr]
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError:
        print('Falta docling: pip install -e ".[docling]"', file=sys.stderr)
        raise SystemExit(2)

    opciones = PdfPipelineOptions()
    opciones.do_ocr = do_ocr
    opciones.do_table_structure = True
    # ACCURATE es más lento que FAST y es el modo que hay que medir: la pregunta
    # es si Docling iguala a Textract en tablas, no si es rápido perdiéndolas.
    opciones.table_structure_options.mode = TableFormerMode.ACCURATE
    convertidor = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opciones)}
    )
    _CONVERTIDORES[do_ocr] = convertidor
    return convertidor


def pdf_docling(
    ruta: Path,
    *,
    banned: tuple[str, ...] = (),
    ocr=None,
    min_chars: int = MINIMO_TEXTO,
    cleanup: CleanupPolicy | None = None,
    do_ocr: bool = False,
    **_,
) -> Documento | None:
    """Misma firma que `extractors.pdf`, para que `pipeline.extraer` no note el cambio.

    `cleanup` se recibe y se ignora: que Docling se deshaga solo de folios y
    encabezados es justamente lo que se está midiendo, y pasarle además la
    limpieza heurística mezclaría los dos efectos.

    `ocr` (el transcriptor externo del pipeline, Textract o tesseract) se usa
    solo como último recurso, igual que en el extractor actual: primero se ve
    qué saca Docling por su cuenta.
    """
    convertidor = _convertidor(do_ocr)

    inicio = time.perf_counter()
    resultado = convertidor.convert(str(ruta))
    segundos = time.perf_counter() - inicio

    documento_docling = resultado.document
    texto = documento_docling.export_to_markdown().strip()
    paginas = len(getattr(documento_docling, "pages", ()) or ()) or None
    tablas = len(getattr(documento_docling, "tables", ()) or ())
    origen = "docling:ocr" if do_ocr else "docling"
    confianza = None

    if len(texto) < min_chars and ocr is not None:
        transcrito = ocr(ruta)
        if transcrito is not None and len(transcrito.texto.strip()) >= min_chars:
            texto = transcrito.texto.strip()
            origen = f"ocr:{transcrito.motor}"
            confianza = transcrito.confianza

    # Se indexa por ruta y no por nombre: dos marcas pueden llamar igual a su
    # ficha, y contarlas como una escondía un documento del reporte.
    MEDICIONES[str(ruta)] = {
        "archivo": ruta.name,
        "ruta": str(ruta),
        "segundos": round(segundos, 2),
        "paginas": paginas,
        "caracteres": len(texto),
        "tablas": tablas,
        "origen_texto": origen,
        "estado": str(getattr(resultado, "status", "")),
    }

    if (marcador := marcador_vetado(texto, banned)):
        raise VetadoError(marcador)
    if len(texto) < min_chars:
        return None

    metadata: dict = {
        "tipo": clasificar(ruta.stem),
        "fuente": ruta.name,
        "paginas": paginas if paginas is not None else 0,
        "origen_texto": origen,
        # Claves propias del laboratorio: el comparador las lee del corpus ya
        # escrito, sin depender de que el reporte JSON siga por ahí.
        "docling_segundos": round(segundos, 2),
        "docling_tablas": tablas,
    }
    if confianza is not None:
        metadata["ocr_confianza"] = confianza
    if (anio := anio_en(slug(ruta.stem))):
        metadata["anio"] = anio
    return Documento(
        nombre=f"{slug(ruta.stem)}.md",
        texto=f"# {ruta.stem}\n\n{texto}\n",
        metadata=metadata,
    )


def instalar(*, do_ocr: bool = False) -> None:
    """Pone el extractor de Docling en el registro, en lugar del de pypdf.

    Un parche en caliente, y deliberadamente: el módulo de producción no gana
    una rama `if docling` por un experimento que puede acabar en la basura.
    """
    from functools import partial

    from rag_agent.infrastructure.ingest import extractors

    extractors.POR_EXTENSION[".pdf"] = (partial(pdf_docling, do_ocr=do_ocr),)
