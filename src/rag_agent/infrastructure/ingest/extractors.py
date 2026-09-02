"""Extractores: de un formato de archivo a texto plano con metadatos.

Cada extractor se identifica a sí mismo por el contenido, no por configuración:
un XML solo se trata como cédula electrónica si su raíz lo es, y un JSON solo
como actividad de GitHub si trae los campos de contribuciones. Así un perfil
nuevo no tiene que declarar qué extractores usar — le sirven todos, y ninguno
se activa donde no toca.

El extractor genérico de PDF es el que sostiene el caso general: un folleto de
coche, un informe de inversiones o cualquier documento con capa de texto. Cuando
esa capa no existe o está cifrada, hay dos recursos antes de darse por vencido:

1. **Descifrar.** Muchos PDF corporativos vienen con cifrado AES y contraseña de
   propietario vacía —restringen copiar e imprimir, no leer— y su texto está
   ahí, entero. Es el rescate más barato que existe: seis fichas de este corpus
   volvieron con 14.000-19.000 caracteres cada una sin transcribir nada.
2. **Transcribir.** Solo si de verdad no hay texto. Ver `ocr/`.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .documents import Documento, VetadoError, anio_en, clasificar, marcador_vetado, slug

# Líneas de sello, huella o base64: ruido que degrada la recuperación.
_RUIDO = re.compile(r"^[A-Za-z0-9+/=]{60,}$")
# Número de página suelto: en un folleto de 40 páginas son 40 líneas de basura.
_NUMERO_DE_PAGINA = re.compile(r"^\s*[-–—]?\s*\d{1,3}\s*[-–—]?\s*$")
_MESES = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()

# Por debajo de esto el PDF no tiene capa de texto útil: es un escaneo y
# necesita OCR o transcripción manual.
MINIMO_TEXTO = 200


def limpiar(paginas: list[str]) -> str:
    lineas = [l.rstrip() for pagina in paginas for l in pagina.splitlines()]
    utiles = [
        l for l in lineas
        if l.strip() and not _RUIDO.match(l.strip()) and not _NUMERO_DE_PAGINA.match(l)
    ]
    # Los folletos repiten el pie de página en cada plana. Colapsar líneas
    # idénticas consecutivas quita la repetición sin tocar contenido real.
    colapsadas: list[str] = []
    for linea in utiles:
        if not colapsadas or colapsadas[-1] != linea:
            colapsadas.append(linea)
    return "\n".join(colapsadas).strip()


def pdf(
    ruta: Path,
    *,
    banned: tuple[str, ...] = (),
    ocr=None,
    min_chars: int = MINIMO_TEXTO,
    **_,
) -> Documento | None:
    """Texto del PDF: capa nativa, descifrando si hace falta, y OCR como respaldo.

    `ocr` es un invocable `Path -> ResultadoOcr | None`. Se le pasa el documento
    entero y no las páginas ya rasterizadas porque quien lo provee es el
    pipeline, que es el que sabe del caché y del tope de páginas.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        print('Falta pypdf: pip install -e ".[ingest]"', file=sys.stderr)
        raise SystemExit(2)

    lector = PdfReader(ruta)
    cifrado = bool(lector.is_encrypted)
    if cifrado:
        # Contraseña vacía: el caso habitual del PDF que restringe copiar e
        # imprimir pero no leer. Si tampoco así se abre, se deja que falle: un
        # documento realmente protegido no debe entrar al corpus a la fuerza.
        lector.decrypt("")

    paginas = [p.extract_text() or "" for p in lector.pages]
    texto = limpiar(paginas)
    origen = "capa de texto"
    confianza = None

    if len(texto) < min_chars and ocr is not None:
        transcrito = ocr(ruta)
        if transcrito is not None and len(transcrito.texto.strip()) >= min_chars:
            texto = transcrito.texto.strip()
            origen = f"ocr:{transcrito.motor}"
            confianza = transcrito.confianza

    if (marcador := marcador_vetado(texto, banned)):
        raise VetadoError(marcador)
    if len(texto) < min_chars:
        return None

    metadata: dict = {
        "tipo": clasificar(ruta.stem),
        "fuente": ruta.name,
        "paginas": len(paginas),
        # De dónde salió el texto viaja con el fragmento: una cita que procede
        # de una transcripción automática no vale lo mismo que una del original,
        # y quien audite la respuesta tiene derecho a saberlo sin abrir el PDF.
        "origen_texto": origen,
    }
    if cifrado:
        metadata["cifrado_original"] = True
    if confianza is not None:
        metadata["ocr_confianza"] = confianza
    if (anio := anio_en(slug(ruta.stem))):
        metadata["anio"] = anio
    return Documento(nombre=f"{slug(ruta.stem)}.md", texto=f"# {ruta.stem}\n\n{texto}\n", metadata=metadata)


def cedula_electronica(ruta: Path, *, banned: tuple[str, ...] = (), **_) -> Documento | None:
    """Cédula profesional electrónica de la SEP (XML firmado).

    Se prefiere el XML al PDF: el PDF trae el mismo dato entre sellos en
    base64, que ensucian el fragmento sin aportar nada recuperable.
    """
    raiz = ET.parse(ruta).getroot()
    if raiz.tag != "CedulaElectronica":
        return None

    ced = raiz.find("Cedula").attrib
    prof = raiz.find("Profesionista").attrib
    inst = raiz.find("Institucion").attrib
    carrera = raiz.find("Carrera").attrib

    fecha = ced["fechaExpedicion"].split()[0]
    dia, mes, anio = (int(x) for x in fecha.split("/"))
    nombre_completo = f"{prof['nombre']} {prof['primerApellido']} {prof['segundoApellido']}".title()
    carrera_nombre = carrera["nombreCarrera"].capitalize()
    institucion = inst["nombreInstitucion"].title()

    texto = f"""# Cédula profesional — {carrera_nombre}

Documento oficial: cédula profesional electrónica expedida por la Dirección
General de Profesiones de la Secretaría de Educación Pública (SEP).

- Titular: {nombre_completo}
- Profesión registrada: {carrera_nombre}
- Institución que expidió el título: {institucion}
- Número de cédula profesional: {ced['numeroCedula']}
- Fecha de expedición: {dia} de {_MESES[mes - 1]} de {anio}
- Entidad federativa: {ced['entidadFederativa'].title()}
- Estado: vigente y registrada ante la Dirección General de Profesiones
- CURP del titular: {prof['curp']}
- Libro {ced['libroCedula']}, foja {ced['fojaCedula']}, número {ced['numeroFojaLibro']}, tipo {ced['Tipo']}

{nombre_completo} está titulado: cuenta con el título profesional de
{carrera_nombre}, expedido por {institucion}, y con la cédula profesional
correspondiente registrada ante la SEP. Su formación académica y su profesión
son las indicadas arriba.

La cédula profesional acredita el ejercicio profesional de la carrera indicada
y presupone el título correspondiente expedido por la institución educativa.
"""
    return Documento(
        nombre=f"cedula-profesional-{slug(carrera_nombre)}-{anio}.md",
        texto=texto,
        metadata={
            "tipo": "cedula",
            "anio": anio,
            "institucion": institucion,
            "carrera": carrera_nombre,
            "emisor": "SEP — Dirección General de Profesiones",
            "fuente": ruta.name,
        },
    )


def actividad_github(
    ruta: Path, *, incluir_repos: bool = False, banned: tuple[str, ...] = (), **_
) -> Documento | None:
    """Perfil público de GitHub: actividad de desarrollo por año.

    Los nombres de repositorio privados quedan fuera salvo petición explícita:
    el agente puede acreditar volumen y constancia sin exponer en qué trabaja
    un cliente.
    """
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if not isinstance(datos, dict):
        # Un JSON cualquiera del corpus (un catálogo, un volcado) no es esto.
        return None
    usuario = datos.get("data", {})
    usuario = usuario.get("user", datos) if isinstance(usuario, dict) else datos
    if not isinstance(usuario, dict):
        return None
    anios = sorted((k for k in usuario if k.startswith("year")), reverse=True)
    if not anios:
        return None

    filas = ["| Año | Commits | Pull requests | Repos con actividad |", "|---|---|---|---|"]
    total_commits = 0
    detalle: list[str] = []
    for clave in anios:
        y = usuario[clave]
        anio = clave.removeprefix("year")
        commits = y.get("totalCommitContributions", 0)
        total_commits += commits
        filas.append(
            f"| {anio} | {commits} | {y.get('totalPullRequestContributions', 0)} "
            f"| {y.get('totalRepositoriesWithContributedCommits', 0)} |"
        )
        if incluir_repos:
            repos = ", ".join(
                f"{r['repository']['name']} ({r['contributions']['totalCount']})"
                for r in y.get("commitContributionsByRepository", [])
            )
            if repos:
                detalle.append(f"- {anio}: {repos}")

    texto = f"""# Actividad de desarrollo en GitHub

Registro público de contribuciones de la cuenta de GitHub del titular.
Cubre de {anios[-1].removeprefix('year')} a {anios[0].removeprefix('year')}, con
{total_commits} commits acumulados.

{chr(10).join(filas)}

Esta actividad es evidencia de constancia y volumen de desarrollo; no acredita
titulación ni certificación alguna.
"""
    if detalle:
        texto += "\n## Repositorios con más actividad por año\n\n" + "\n".join(detalle) + "\n"

    return Documento(
        nombre="actividad-github.md",
        texto=texto,
        metadata={
            "tipo": "actividad_publica",
            "anio": int(anios[0].removeprefix("year")),
            "emisor": "GitHub",
            "fuente": ruta.name,
        },
    )


def texto_plano(ruta: Path, *, banned: tuple[str, ...] = (), **_) -> Documento | None:
    """Markdown o texto ya legible: entra tal cual, con sus metadatos laterales."""
    contenido = ruta.read_text(encoding="utf-8").strip()
    if (marcador := marcador_vetado(contenido, banned)):
        raise VetadoError(marcador)
    if not contenido:
        return None
    lateral = ruta.with_suffix(ruta.suffix + ".metadata.json")
    metadata: dict = {"tipo": clasificar(ruta.stem), "fuente": ruta.name}
    if lateral.is_file():
        crudo = json.loads(lateral.read_text(encoding="utf-8"))
        metadata = crudo.get("metadataAttributes", crudo)
    return Documento(nombre=f"{slug(ruta.stem)}.md", texto=contenido, metadata=dict(metadata))


# Orden de intento por extensión. El primero que devuelve algo gana.
POR_EXTENSION: dict[str, tuple] = {
    ".pdf": (pdf,),
    ".xml": (cedula_electronica,),
    ".json": (actividad_github,),
    ".md": (texto_plano,),
    ".markdown": (texto_plano,),
    ".txt": (texto_plano,),
}

EXTENSIONES = tuple(POR_EXTENSION)
