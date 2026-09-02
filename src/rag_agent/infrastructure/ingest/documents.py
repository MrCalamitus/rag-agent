"""Documento extraído: el paso intermedio entre un archivo y un fragmento.

Separado de los extractores a propósito. Un extractor sabe leer un formato; lo
que hace que el resultado sea *utilizable* —que el nombre sea citable, que no
lleve un identificador, que traiga metadatos coherentes— es política, es igual
para todos los formatos, y por eso vive aquí.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Documento:
    """Texto normalizado de un archivo de origen, aún sin trocear."""

    nombre: str
    texto: str
    metadata: dict = field(default_factory=dict)


class VetadoError(Exception):
    """El documento contiene material que el perfil prohíbe indexar."""


def slug(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", texto.lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", plano)).strip("-")


def marcador_vetado(texto: str, marcadores: tuple[str, ...]) -> str | None:
    """Primer marcador prohibido que aparece en el texto, o `None`.

    Se busca por contenido y no por nombre de archivo: el nombre se puede
    cambiar, el contenido no. En el perfil de credenciales esto es lo que
    impide que una credencial de elector entre al corpus.
    """
    if not marcadores:
        return None
    plano = texto.upper()
    for marcador in marcadores:
        if marcador.upper() in plano:
            return marcador
    return None


def anio_en(nombre: str) -> int | None:
    encontrado = re.search(r"(19|20)\d{2}", nombre)
    return int(encontrado.group(0)) if encontrado else None


def clasificar(stem: str) -> str:
    """Tipo inferido del nombre. Es una pista para filtrar, no una garantía."""
    nombre = slug(stem)
    if nombre.startswith("cv"):
        return "cv"
    if "certificado" in nombre or "constancia" in nombre:
        return "certificado"
    if "titulo" in nombre:
        return "titulo"
    if "ficha" in nombre or "especificacion" in nombre:
        return "ficha_tecnica"
    if "folleto" in nombre or "catalogo" in nombre or "brochure" in nombre:
        return "folleto"
    return "documento"


def metadata_de_ruta(archivo: Path, origen: Path, campos: tuple[str, ...]) -> dict[str, str]:
    """Convierte los tramos de la ruta en metadatos, según declare el perfil.

    Con `corpus/toyota/hilux-2024.pdf`, origen `corpus` y campos `("marca",)`
    sale `{"marca": "toyota"}`. Es la forma más barata de tener metadatos
    útiles: la carpeta ya expresa la organización que el usuario le dio a sus
    documentos, y filtrar por marca no debería exigir editar nada.
    """
    if not campos:
        return {}
    try:
        tramos = archivo.resolve().relative_to(origen.resolve()).parts[:-1]
    except ValueError:
        return {}
    return {campo: slug(tramo) for campo, tramo in zip(campos, tramos)}
