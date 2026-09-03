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
from math import ceil
from pathlib import Path

from ...domain.profile import CleanupPolicy

from .paginas import seleccionar
from .documents import Documento, VetadoError, anio_en, clasificar, marcador_vetado, slug

# Líneas de sello, huella o base64: ruido que degrada la recuperación.
_RUIDO = re.compile(r"^[A-Za-z0-9+/=]{60,}$")
# Folio: un número suelto, con o sin guiones de adorno. **Solo se descarta si
# está en el borde de su página.** Aplicarlo en cualquier posición borraba los
# valores de toda tabla cuya capa de texto pone etiqueta y cifra en líneas
# separadas, que es como muchos PDF disponen sus tablas.
_NUMERO_DE_PAGINA = re.compile(r"^\s*[-–—]?\s*\d{1,3}\s*[-–—]?\s*$")
# «Página 4 de 12», «Pág. 4/12»: inequívocamente un folio, en cualquier posición.
_FOLIO_EXPLICITO = re.compile(
    r"^\s*(p[áa]g(?:ina)?\.?)\s*\d+\s*(?:(?:de|/|of)\s*\d+)?\s*$", re.IGNORECASE
)
_MESES = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()

# Por debajo de esto el PDF no tiene capa de texto útil: es un escaneo y
# necesita OCR o transcripción manual.
MINIMO_TEXTO = 200


def limpiar(paginas: list[str], policy: CleanupPolicy | None = None) -> str:
    """Quita folios y encabezados corridos sin tocar el contenido.

    Se trabaja **por páginas** y no sobre el texto aplanado, porque las dos
    señales que distinguen la basura del dato son posicionales: un folio está en
    el borde de su página, y un pie corrido se repite a lo largo del documento.
    Sin esa información las dos reglas degeneran en «borra números» y «borra
    repeticiones», que sobre una tabla numérica es borrar la tabla.
    """
    politica = policy or CleanupPolicy()
    por_pagina: list[list[str]] = []
    for pagina in paginas:
        lineas = [l.rstrip() for l in pagina.splitlines()]
        por_pagina.append([l for l in lineas if l.strip() and not _RUIDO.match(l.strip())])

    corridas = _lineas_corridas(por_pagina, politica)
    folios = _folios_posicionales(por_pagina, politica)
    # De una línea corrida se conserva la primera aparición y se quitan las
    # demás. Suele ser el título o el modelo —«MAZDA2 SEDÁN 2026»— y borrarlo de
    # todas partes deja los fragmentos del medio del documento sin decir de qué
    # hablan. Una vez basta; cuatrocientas treinta y nueve, no.
    vistas: set[str] = set()
    salida: list[str] = []
    for pagina, lineas in enumerate(por_pagina):
        for indice, linea in enumerate(lineas):
            if (pagina, indice) in folios or _FOLIO_EXPLICITO.match(linea):
                continue
            if linea in corridas:
                if linea in vistas:
                    continue
                vistas.add(linea)
            salida.append(linea)
    return "\n".join(salida).strip()


def _folios_posicionales(por_pagina: list[list[str]], policy: CleanupPolicy) -> set[tuple[int, int]]:
    """Números sueltos en el borde que además **crecen** de página en página.

    La posición sola no basta: un balance puede acabar una página en «0», y
    borrarlo sería borrar un dato. Un folio de verdad forma una serie creciente a
    lo largo del documento, y comprobarlo cuesta nada. Con una sola página no hay
    serie que comprobar, así que la regla no se aplica —solo queda el folio
    escrito con todas las letras, que es inequívoco.
    """
    borde = policy.folio_lineas_borde
    # Se reutiliza el umbral de páginas de la deduplicación: expresa la misma
    # idea —por debajo de esas páginas ninguna señal estadística o posicional es
    # de fiar— y evita un segundo botón que ajustar.
    if borde <= 0 or len(por_pagina) < policy.repeticion_min_paginas:
        return set()

    folios: set[tuple[int, int]] = set()
    for al_final in (False, True):
        candidatos: list[tuple[int, int, int]] = []  # (página, línea, valor)
        for pagina, lineas in enumerate(por_pagina):
            if len(lineas) <= borde:
                continue
            indice = len(lineas) - 1 if al_final else 0
            texto = lineas[indice]
            if _NUMERO_DE_PAGINA.match(texto):
                candidatos.append((pagina, indice, int(re.sub(r"[^\d]", "", texto) or 0)))
        valores = [v for _, _, v in candidatos]
        if len(candidatos) >= policy.repeticion_min_paginas and all(
            a < b for a, b in zip(valores, valores[1:])
        ):
            folios.update((p, i) for p, i, _ in candidatos)
    return folios


def _lineas_corridas(por_pagina: list[list[str]], policy: CleanupPolicy) -> set[str]:
    """Encabezados y pies que se repiten a lo largo del documento.

    Tres condiciones, y las tres hacen falta. La línea tiene que repetirse en
    una fracción alta de las páginas —no estar dos veces seguidas—, aparecer en
    el **borde** de ellas, que es donde viven encabezados y pies, y no ser un
    número suelto: un `0` al final de cada página de un balance es un dato, y de
    los folios ya se encarga la regla posicional con su comprobación de serie
    creciente.
    """
    if len(por_pagina) < policy.repeticion_min_paginas:
        return set()
    borde = policy.repeticion_lineas_borde
    conteo: dict[str, int] = {}
    for lineas in por_pagina:
        en_borde = {
            linea
            for indice, linea in enumerate(lineas)
            if (indice < borde or indice >= len(lineas) - borde)
            and not _NUMERO_DE_PAGINA.match(linea)
        }
        for linea in en_borde:
            conteo[linea] = conteo.get(linea, 0) + 1
    minimo = max(2, ceil(policy.repeticion_fraccion * len(por_pagina)))
    return {linea for linea, veces in conteo.items() if veces >= minimo}


def pdf(
    ruta: Path,
    *,
    banned: tuple[str, ...] = (),
    ocr=None,
    min_chars: int = MINIMO_TEXTO,
    cleanup: CleanupPolicy | None = None,
    paginas: str = "sin-texto",
    **_,
) -> Documento | None:
    """Texto del PDF: capa nativa, descifrando si hace falta, y el motor encima.

    `ocr` es un invocable `(Path, selección) -> ResultadoOcr | None`. Se le pasa
    el documento entero y no las páginas ya rasterizadas porque quien lo provee
    es el pipeline, que es el que sabe del caché y del tope de páginas.

    `paginas` decide cuándo entra el motor. Históricamente solo entraba si el
    PDF no daba texto, lo que dejaba fuera al informe financiero nativo: tiene
    capa de texto completa, pero `pypdf` devuelve sus tablas aplanadas en una
    columna de cifras sin fila ni encabezado. Con `todas` o `con-tablas` el
    motor corre también ahí y su versión **sustituye página a página** a la
    nativa, que es lo que conserva a qué fila y columna pertenece cada valor.
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

    por_pagina = [p.extract_text() or "" for p in lector.pages]
    texto = limpiar(por_pagina, cleanup)
    origen = "capa de texto"
    confianza = None
    reforzadas = 0

    sin_capa = len(texto) < min_chars
    if ocr is not None and sin_capa:
        transcrito = ocr(ruta)
        if transcrito is not None and len(transcrito.texto.strip()) >= min_chars:
            texto = transcrito.texto.strip()
            origen = f"ocr:{transcrito.motor}"
            confianza = transcrito.confianza
    elif ocr is not None and paginas in ("todas", "con-tablas"):
        seleccion = seleccionar(ruta, paginas, total=len(por_pagina))
        # Sin exigir densidad: aquí la transcripción no es la única fuente sino
        # un refuerzo, y una página con pocas palabras es eso y no un fallo.
        transcrito = ocr(ruta, sorted(seleccion), exigir_densidad=False)
        if transcrito is not None:
            for pagina in transcrito.paginas:
                indice = pagina.numero - 1
                if pagina.texto.strip() and 0 <= indice < len(por_pagina):
                    por_pagina[indice] = pagina.texto
                    reforzadas += 1
            if reforzadas:
                texto = limpiar(por_pagina, cleanup)
                origen = (
                    f"capa de texto + {transcrito.motor} "
                    f"({reforzadas}/{len(por_pagina)} páginas)"
                )
                confianza = transcrito.confianza

    if (marcador := marcador_vetado(texto, banned)):
        raise VetadoError(marcador)
    if len(texto) < min_chars:
        return None

    metadata: dict = {
        "tipo": clasificar(ruta.stem),
        "fuente": ruta.name,
        "paginas": len(por_pagina),
        # De dónde salió el texto viaja con el fragmento: una cita que procede
        # de una transcripción automática no vale lo mismo que una del original,
        # y quien audite la respuesta tiene derecho a saberlo sin abrir el PDF.
        "origen_texto": origen,
    }
    if cifrado:
        metadata["cifrado_original"] = True
    if confianza is not None:
        metadata["ocr_confianza"] = confianza
    if reforzadas:
        metadata["paginas_con_estructura"] = reforzadas
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
