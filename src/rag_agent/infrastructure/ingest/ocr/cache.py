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

VERSION = 1


def clave(pdf: Path, *, motor: str, dpi: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"v{VERSION}|{motor}|{dpi}|".encode())
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
