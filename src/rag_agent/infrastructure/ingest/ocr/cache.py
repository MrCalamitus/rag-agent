"""Caché de transcripciones.

El OCR es lento y, con un motor en la nube, se paga por página. Sin caché,
cualquier reajuste del troceado —cambiar `max_chars` y volver a lanzar
`make corpus`— vuelve a transcribir el corpus entero y a pagarlo otra vez.

La clave incluye el **contenido** del PDF, no su ruta ni su fecha: si el archivo
cambia se vuelve a transcribir, y si solo se movió de carpeta no. También
incluye motor y resolución, porque el resultado depende de ambos.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from . import PaginaExtraida, ResultadoOcr

# Se sube cuando cambia **cómo se redacta** la transcripción, no solo cómo se
# obtiene: el caché guarda el texto ya redactado, así que un render nuevo sobre
# un caché viejo mezclaría dos formatos en el mismo corpus. Subirla obliga a
# volver a transcribir, que cuesta dinero — es el precio de guardar texto en vez
# de la respuesta cruda del motor, y está anotado como mejora pendiente.
#
#   1 → primera versión
#   2 → detección de forma de tabla y volcado genérico; «columnas» del perfil
#   3 → no se inventan encabezados sobre maquetaciones que no son tablas
VERSION = 3


def clave(pdf: Path, *, motor: str, dpi: int, seleccion: str = "") -> str:
    """Huella del contenido más los ajustes que cambian el resultado.

    `seleccion` entra en la huella porque una transcripción de tres páginas y
    otra de ochenta y cinco no son el mismo resultado: sin distinguirlas, una
    prueba acotada dejaría cacheado un documento a medias como si fuera el
    definitivo.
    """
    digest = hashlib.sha256()
    digest.update(f"v{VERSION}|{motor}|{dpi}|{seleccion}|".encode())
    with pdf.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            digest.update(bloque)
    return digest.hexdigest()[:32]


def leer(carpeta: Path, clave_: str) -> ResultadoOcr | None:
    ruta = carpeta / f"{clave_}.json"
    if not ruta.is_file():
        return None
    try:
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
        return ResultadoOcr(
            motor=crudo["motor"],
            paginas=[PaginaExtraida(**p) for p in crudo["paginas"]],
            avisos=list(crudo.get("avisos", [])),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        # Un caché corrupto no debe romper la ingesta: se ignora y se rehace.
        return None


def escribir(carpeta: Path, clave_: str, resultado: ResultadoOcr) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"{clave_}.json").write_text(
        json.dumps(
            {
                "motor": resultado.motor,
                "avisos": resultado.avisos,
                "paginas": [asdict(p) for p in resultado.paginas],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
