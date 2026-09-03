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

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.chunking import chunk_document_id, split
from ...domain.profile import Profile
from .documents import Documento, VetadoError, metadata_de_ruta
from .extractors import EXTENSIONES, POR_EXTENSION, MINIMO_TEXTO
from .ocr import MotorOcr, ResultadoOcr, build_motor
from .ocr import cache as ocr_cache
from .ocr.rasterize import contar_paginas, rasterizar, rasterizar_seleccion

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
    # Documentos rescatados por transcripción, con su motor y confianza.
    transcritos: list[tuple[str, str, float | None]] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

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


def extraer(archivo: Path, profile: Profile, ocr=None) -> Documento | None:
    for extractor in POR_EXTENSION.get(archivo.suffix.lower(), ()):
        documento = extractor(
            archivo,
            banned=profile.banned_markers,
            ocr=ocr,
            min_chars=profile.ocr.min_chars,
            cleanup=profile.cleanup,
            paginas=profile.ocr.paginas,
        )
        if documento is not None:
            return documento
    return None


def _renumerar(resultado: ResultadoOcr, numeros: Sequence[int]) -> None:
    """Reasigna el número real de página a cada página transcrita."""
    from dataclasses import replace

    resultado.paginas = [
        replace(pagina, numero=numeros[indice]) if indice < len(numeros) else pagina
        for indice, pagina in enumerate(resultado.paginas)
    ]


class Transcriptor:
    """Transcribe un PDF, una sola vez.

    El caché no es una optimización: con un motor que cobra por página, ajustar
    el troceado y relanzar `make corpus` volvería a pagar la transcripción
    entera. La clave va por contenido del archivo, así que un PDF que cambia se
    vuelve a transcribir y uno que solo se movió de carpeta no.
    """

    def __init__(
        self,
        motor: MotorOcr,
        policy,
        carpeta_cache: Path,
        *,
        reporte: Reporte | None = None,
    ) -> None:
        self._motor = motor
        self._policy = policy
        self._cache = carpeta_cache
        self._reporte = reporte

    def __call__(
        self,
        ruta: Path,
        paginas: Sequence[int] | None = None,
        *,
        exigir_densidad: bool = True,
    ) -> ResultadoOcr | None:
        """Transcribe el documento, o solo las páginas indicadas.

        `exigir_densidad` se apaga cuando la transcripción no es la única fuente
        sino un refuerzo sobre la capa de texto: ahí una página con pocas
        palabras es una página con pocas palabras, no una transcripción fallida,
        y descartarla tiraría las tablas bien extraídas del resto.
        """
        seleccion = sorted(set(paginas)) if paginas is not None else None
        etiqueta = ",".join(map(str, seleccion)) if seleccion is not None else ""
        clave = ocr_cache.clave(
            ruta, motor=self._motor.nombre, dpi=self._policy.dpi, seleccion=etiqueta
        )
        guardado = ocr_cache.leer(self._cache, clave)
        if guardado is not None:
            self._anotar(ruta, guardado, cacheado=True)
            return self._con_densidad_suficiente(ruta, guardado, exigir=exigir_densidad)

        if seleccion is None:
            imagenes = [
                (numero, png)
                for numero, png in enumerate(
                    rasterizar(
                        ruta, dpi=self._policy.dpi, max_paginas=self._policy.max_paginas
                    ),
                    start=1,
                )
            ]
        else:
            imagenes = rasterizar_seleccion(
                ruta, seleccion[: self._policy.max_paginas], dpi=self._policy.dpi
            )
        if not imagenes:
            return None
        resultado = self._motor.extraer([png for _, png in imagenes], idioma=self._policy.idioma)
        # El motor numera 1..N sobre lo que recibió; con una selección salteada
        # eso no son las páginas del documento. Se renumeran aquí, que es donde
        # se sabe cuáles se pidieron.
        _renumerar(resultado, [numero for numero, _ in imagenes])
        if len(resultado.paginas) < len(imagenes):
            # Transcripción incompleta: alguna página se cayó por red o por
            # estrangulamiento del servicio. **No se cachea.** Guardar un
            # resultado parcial convierte un fallo pasajero en la versión
            # definitiva del documento, y nadie volvería a mirarlo: el corpus
            # se quedaría con media ficha para siempre.
            resultado.avisos.append(
                f"transcripción incompleta ({len(resultado.paginas)} de {len(imagenes)} "
                f"páginas): no se guarda en caché, se reintentará en la próxima ejecución"
            )
            self._anotar(ruta, resultado, cacheado=False)
            return resultado
        total = contar_paginas(ruta)
        if total > self._policy.max_paginas:
            resultado.avisos.append(
                f"solo se transcribieron {self._policy.max_paginas} de {total} páginas "
                f"(tope del perfil: max_paginas)"
            )
        # Se cachea aunque luego se rechace por densidad: la transcripción está
        # completa y volver a pedirla costaría lo mismo y daría lo mismo.
        ocr_cache.escribir(self._cache, clave, resultado)
        self._anotar(ruta, resultado, cacheado=False)
        return self._con_densidad_suficiente(ruta, resultado, exigir=exigir_densidad)

    def _con_densidad_suficiente(
        self, ruta: Path, resultado: ResultadoOcr, *, exigir: bool = True
    ) -> ResultadoOcr | None:
        """Descarta la transcripción que salió demasiado pobre para ser evidencia.

        Un PDF de catorce páginas del que salen 900 caracteres no se transcribió
        mal: es que no contiene lo que su nombre promete. Indexarlo es peor que
        descartarlo, porque el agente lo citaría con toda propiedad para
        responder algo que ese documento no dice.
        """
        if not exigir:
            return resultado
        paginas = len(resultado.paginas) or 1
        densidad = len(resultado.texto.strip()) / paginas
        if densidad >= self._policy.min_chars_por_pagina:
            return resultado
        if self._reporte is not None:
            self._reporte.avisos.append(
                f"{ruta.name}: transcripción descartada — {densidad:.0f} caracteres por "
                f"página sobre {paginas} (mínimo {self._policy.min_chars_por_pagina}). "
                f"El PDF no contiene el texto que su nombre sugiere."
            )
            self._reporte.transcritos = [
                t for t in self._reporte.transcritos if t[0] != ruta.name
            ]
        return None

    def _anotar(self, ruta: Path, resultado: ResultadoOcr, *, cacheado: bool) -> None:
        if self._reporte is None:
            return
        # Los avisos se propagan SIEMPRE, con texto o sin él. Descartarlos
        # cuando la transcripción vuelve vacía era esconder el diagnóstico justo
        # en el caso que hay que diagnosticar: un lote entero puede fallar por
        # estrangulamiento del servicio y el reporte decía solo «no dio
        # resultado», sin nombrar la causa.
        for aviso in resultado.avisos:
            mensaje = f"{ruta.name}: {aviso}"
            if mensaje not in self._reporte.avisos:
                self._reporte.avisos.append(mensaje)
        if resultado.texto.strip():
            self._reporte.transcritos.append((ruta.name, resultado.motor, resultado.confianza))


@dataclass(frozen=True)
class Candidato:
    """Un PDF que necesitaría transcripción, y lo que costaría."""

    ruta: Path
    paginas: int
    en_cache: bool


def escanear_ocr(origen: Path, profile: Profile, carpeta_cache: Path) -> list[Candidato]:
    """Qué documentos habría que transcribir, antes de transcribir ninguno.

    Existe para poder avisar del costo *antes* de gastarlo. Un motor en la nube
    cobra por página, y enterarse a mitad de un lote de cien no sirve de nada.
    """
    from .extractors import limpiar

    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []

    candidatos: list[Candidato] = []
    for ruta in sorted(origen.rglob("*.pdf")):
        try:
            lector = PdfReader(ruta)
            if lector.is_encrypted:
                lector.decrypt("")
            texto = limpiar([p.extract_text() or "" for p in lector.pages], profile.cleanup)
        except Exception:  # noqa: BLE001 - un PDF ilegible se reporta en la preparación
            continue
        if len(texto) >= profile.ocr.min_chars:
            continue
        clave = ocr_cache.clave(ruta, motor=profile.ocr.motor, dpi=profile.ocr.dpi)
        candidatos.append(
            Candidato(
                ruta=ruta,
                paginas=min(contar_paginas(ruta), profile.ocr.max_paginas),
                en_cache=ocr_cache.leer(carpeta_cache, clave) is not None,
            )
        )
    return candidatos


def _ruta_relativa(archivo: Path, origen: Path) -> str:
    """Ruta con la que se comparan los patrones `rutas` de una clase.

    Siempre con `/`, para que un patrón escrito en el YAML signifique lo mismo
    en macOS y en el contenedor Linux donde corre la ingesta de verdad.
    """
    try:
        return archivo.relative_to(origen).as_posix()
    except ValueError:
        return archivo.name


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
    carpeta_cache: Path | None = None,
    ocr: bool = True,
) -> Reporte:
    """Recorre el origen y produce los fragmentos, sin escribir nada todavía."""
    reporte = Reporte()
    transcriptor = None
    if ocr and profile.ocr.activo:
        motor = build_motor(profile.ocr)
        listo, motivo = motor.disponible()
        if listo:
            transcriptor = Transcriptor(
                motor, profile.ocr, carpeta_cache or (origen / ".ocr-cache"), reporte=reporte
            )
        else:
            reporte.avisos.append(f"OCR desactivado: {motivo}")
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
            documento = extraer(archivo, profile, transcriptor)
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
                (
                    "sin capa de texto y la transcripción no dio resultado"
                    if transcriptor is not None
                    else "sin capa de texto útil → activa el OCR en el perfil (ocr.motor)"
                )
                if archivo.suffix.lower() == ".pdf"
                else f"ningún extractor reconoce este {archivo.suffix.lstrip('.')} → conviértelo o usa --skip"
            )
            reporte.sin_texto.append((archivo.name, motivo))
            continue

        reporte.documentos += 1
        extra = metadata_de_ruta(archivo, origen, profile.path_metadata)
        # La clase se decide aquí, con el documento entero delante, y viaja con
        # cada fragmento. Al responder ya no hay forma de saber si un texto
        # suelto venía de un folleto o de una cédula: la ruta se perdió y el
        # documento original no está. Clasificar es de la ingesta; decidir si se
        # expone, del perfil, y eso se evalúa al servir.
        extra["clase"] = profile.documents.clasificar(
            ruta=_ruta_relativa(archivo, origen),
            tipo=str(documento.metadata.get("tipo", "")),
            texto=documento.texto,
        )
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
