"""PDF → imágenes de página.

Se usa **pypdfium2** y no PyMuPDF por licencia: PyMuPDF es AGPL-3.0 y este
proyecto es Apache-2.0. pypdfium2 envuelve PDFium (BSD-3/Apache-2.0) y trae los
binarios en la rueda, así que rasterizar no exige instalar poppler ni
ghostscript en la máquina de quien prepara el corpus.
"""

from __future__ import annotations

import io
from pathlib import Path

# Un PNG de página a 200 ppp ronda 1,5 MB. Los motores en la nube suelen
# rechazar imágenes por encima de 5 MB, así que se recomprime antes de enviarla
# en vez de fallar a mitad de un lote de cien páginas.
MAX_BYTES = 4_500_000


def falta_dependencia(paquete: str) -> str:
    return (
        f"Falta {paquete}. La preparación del corpus tiene dependencias propias: "
        f'pip install -e ".[ingest]"'
    )


def rasterizar(ruta: Path, *, dpi: int = 200, max_paginas: int = 40) -> list[bytes]:
    """Devuelve las páginas como PNG, hasta `max_paginas`."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - depende del entorno
        raise SystemExit(falta_dependencia("pypdfium2")) from None

    documento = pdfium.PdfDocument(str(ruta))
    try:
        paginas: list[bytes] = []
        for indice in range(min(len(documento), max_paginas)):
            imagen = documento[indice].render(scale=dpi / 72).to_pil()
            paginas.append(_a_png(imagen))
        return paginas
    finally:
        documento.close()


def _a_png(imagen) -> bytes:
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG", optimize=True)
    if buffer.tell() <= MAX_BYTES:
        return buffer.getvalue()
    # Reducir escala antes que calidad: el ruido de JPEG sobre texto pequeño
    # perjudica al OCR más que perder algo de resolución.
    ancho, alto = imagen.size
    factor = (MAX_BYTES / buffer.tell()) ** 0.5
    reducida = imagen.resize((max(1, int(ancho * factor)), max(1, int(alto * factor))))
    buffer = io.BytesIO()
    reducida.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def contar_paginas(ruta: Path) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover
        return 0
    documento = pdfium.PdfDocument(str(ruta))
    try:
        return len(documento)
    finally:
        documento.close()
