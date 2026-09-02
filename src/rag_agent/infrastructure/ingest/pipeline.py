"""Ingesta: de una carpeta de originales a un corpus indexable.

    originales/*.pdf → extractor → veto → troceo → corpus/*.md + *.metadata.json

El resultado sirve por igual a la recuperación local y a la ingesta en Bedrock
Knowledge Base: los mismos archivos, los mismos `document_id`, los mismos
metadatos. Que ambos caminos partan del mismo artefacto es lo que hace que
probar en local diga algo sobre lo desplegado.

Dos reglas que este módulo hace cumplir, heredadas de la versión que solo sabía
de credenciales:

1. **Un perfil con material sensible no escribe dentro del repositorio.** Se
   deduce del propio perfil —enmascara identificadores o declara marcadores
   vetados— y no de una constante: un corpus de folletos de coches sí puede
   vivir en el árbol de trabajo, y obligarlo a salir sería fricción sin motivo.
2. **El nombre del archivo es el `document_id` que el agente cita**, así que se
   normaliza a un slug legible y sin identificadores.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.chunking import chunk_document_id, split
from ...domain.profile import Profile
from .documents import Documento, VetadoError, metadata_de_ruta
from .extractors import EXTENSIONES, POR_EXTENSION

REPO = Path(__file__).resolve().parents[4]


@dataclass
class Fragmento:
    """Un archivo del corpus preparado, listo para escribirse."""

    document_id: str
    texto: str
    metadata: dict

    @property
    def nombre(self) -> str:
        return f"{self.document_id}.md"


@dataclass
class Reporte:
    fragmentos: list[Fragmento] = field(default_factory=list)
    documentos: int = 0
    vetados: list[tuple[str, str]] = field(default_factory=list)
    sin_texto: list[tuple[str, str]] = field(default_factory=list)
    omitidos: list[tuple[str, str]] = field(default_factory=list)
    errores: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_fragmentos(self) -> int:
        return len(self.fragmentos)


class DestinoInvalido(ValueError):
    """El destino elegido violaría la regla de no escribir datos en el repo."""


def validar_destino(destino: Path, profile: Profile) -> None:
    if not (profile.masks_identifiers or profile.banned_markers):
        return
    resuelto = destino.resolve()
    if resuelto == REPO or REPO in resuelto.parents:
        raise DestinoInvalido(
            f"El perfil '{profile.slug}' maneja material sensible y el destino {resuelto} "
            f"está dentro del repositorio. Un documento de identidad commiteado no se "
            f"borra con un `rm`: elige una carpeta fuera del árbol de trabajo."
        )


def _archivos(origen: Path, patrones: tuple[str, ...]) -> list[Path]:
    encontrados = {a for patron in patrones for a in origen.rglob(patron) if a.is_file()}
    return sorted(encontrados)


def extraer(archivo: Path, profile: Profile) -> Documento | None:
    for extractor in POR_EXTENSION.get(archivo.suffix.lower(), ()):
        documento = extractor(archivo, banned=profile.banned_markers)
        if documento is not None:
            return documento
    return None


def _fragmentar(documento: Documento, profile: Profile, extra: dict) -> Iterator[Fragmento]:
    stem = documento.nombre.removesuffix(".md")
    trozos = split(documento.texto, profile.chunking)
    for trozo in trozos:
        metadata = {**documento.metadata, **extra}
        if not trozo.is_whole_document:
            # Que el fragmento diga de qué documento y de qué parte viene es lo
            # que permite auditar una cita sin abrir el original.
            metadata |= {"fragmento": trozo.index, "fragmentos_totales": trozo.total}
        if profile.masks_identifiers:
            metadata.setdefault("contiene_pii", True)
        yield Fragmento(
            document_id=chunk_document_id(stem, trozo),
            texto=trozo.text,
            metadata=metadata,
        )


def preparar(
    origen: Path,
    profile: Profile,
    *,
    patrones: tuple[str, ...] = tuple(f"*{ext}" for ext in EXTENSIONES),
    omitir: tuple[str, ...] = (),
) -> Reporte:
    """Recorre el origen y produce los fragmentos, sin escribir nada todavía."""
    reporte = Reporte()
    descartados = {a for patron in omitir for a in origen.rglob(patron)}

    for archivo in _archivos(origen, patrones):
        if archivo in descartados:
            reporte.omitidos.append((archivo.name, "descartado por --skip"))
            continue
        if archivo.name.endswith(".metadata.json"):
            continue
        # Si existe el XML firmado del mismo documento, ese manda: el PDF trae
        # el mismo dato entre sellos en base64.
        if archivo.suffix.lower() == ".pdf" and archivo.with_suffix(".xml").exists():
            reporte.omitidos.append((archivo.name, "se usa su XML firmado"))
            continue

        try:
            documento = extraer(archivo, profile)
        except VetadoError as veto:
            reporte.vetados.append((archivo.name, str(veto)))
            continue
        except Exception as exc:  # noqa: BLE001 - un archivo roto no tumba el lote
            # Con un corpus de decenas de PDFs de origen desconocido, alguno
            # estará cifrado, truncado o no será lo que su extensión dice. Que
            # eso aborte las otras 130 conversiones es el peor comportamiento
            # posible: se reporta y se sigue.
            reporte.errores.append((archivo.name, f"{type(exc).__name__}: {exc}"))
            continue
        if documento is None:
            # Distinguir ambos casos importa: un PDF escaneado se arregla con
            # OCR, un JSON sin extractor no. Decir "requiere OCR" de un JSON
            # manda al usuario a perder una tarde.
            motivo = (
                "sin capa de texto útil → requiere OCR o transcripción"
                if archivo.suffix.lower() == ".pdf"
                else f"ningún extractor reconoce este {archivo.suffix.lstrip('.')} → conviértelo o usa --skip"
            )
            reporte.sin_texto.append((archivo.name, motivo))
            continue

        reporte.documentos += 1
        extra = metadata_de_ruta(archivo, origen, profile.path_metadata)
        reporte.fragmentos.extend(_fragmentar(documento, profile, extra))

    return reporte


def escribir(reporte: Reporte, destino: Path, profile: Profile) -> None:
    """Vuelca los fragmentos y el manifiesto. Valida el destino antes de tocar disco."""
    import json

    validar_destino(destino, profile)
    destino.mkdir(parents=True, exist_ok=True)
    for fragmento in reporte.fragmentos:
        (destino / fragmento.nombre).write_text(fragmento.texto, encoding="utf-8")
        (destino / f"{fragmento.nombre}.metadata.json").write_text(
            json.dumps({"metadataAttributes": fragmento.metadata}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    _manifiesto(reporte, destino)


def _manifiesto(reporte: Reporte, destino: Path) -> None:
    """Índice plano del corpus. Es lo que se revisa antes de subir nada a S3."""
    columnas = ["document_id", "tipo", "anio", "fuente", "caracteres"]
    extra = sorted(
        {k for f in reporte.fragmentos for k in f.metadata} - set(columnas) - {"fragmentos_totales"}
    )
    lineas = [",".join(columnas + extra)]
    for f in reporte.fragmentos:
        valores = [
            f.document_id,
            str(f.metadata.get("tipo", "")),
            str(f.metadata.get("anio", "")),
            str(f.metadata.get("fuente", "")),
            str(len(f.texto)),
        ] + [str(f.metadata.get(k, "")) for k in extra]
        lineas.append(",".join(f'"{v}"' if "," in v else v for v in valores))
    (destino / "manifiesto.csv").write_text("\n".join(lineas) + "\n", encoding="utf-8")
