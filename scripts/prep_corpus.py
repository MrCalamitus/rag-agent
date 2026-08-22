#!/usr/bin/env python3
"""Preparación del corpus (plan E2): fuentes → fragmentos legibles + metadatos.

Convierte documentos de origen en Markdown normalizado con su
`<archivo>.metadata.json` al lado, listo tanto para la recuperación local como
para la ingesta a Bedrock Knowledge Base.

    python scripts/prep_corpus.py --source ~/Desktop/docsLuis --out ~/Desktop/docsLuis/corpus-luis-cv

Dos reglas que este script hace cumplir:

1. **Los datos nunca entran al repositorio.** Si el destino cae dentro del
   repo, aborta. Un documento de identidad commiteado no se borra con un `rm`.
2. **El nombre del archivo es el `document_id` que el agente cita**, así que no
   puede contener un identificador. Un origen llamado `<numero-de-cedula>-C1.pdf`
   se convierte en `cedula-profesional-<carrera>-<anio>.md`: legible en la cita
   y sin filtrar el número a quien solo preguntó por la carrera.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Líneas de sello, huella o base64: ruido que degrada la recuperación.
_RUIDO = re.compile(r"^[A-Za-z0-9+/=]{60,}$")

# Documentos de identidad: nunca entran al corpus. Un agente que responde por
# API no debe poder recitar un domicilio ni una clave de elector, ni siquiera
# enmascarados. Se detecta por contenido, no por nombre de archivo: el nombre
# se puede cambiar, el contenido no.
_VETADOS = (
    "CREDENCIAL PARA VOTAR",
    "CLAVE DE ELECTOR",
    "PASAPORTE",
    "LICENCIA DE CONDUCIR",
)
_MESES = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()


@dataclass
class Documento:
    nombre: str
    texto: str
    metadata: dict = field(default_factory=dict)


def slug(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", texto.lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", plano)).strip("-")


def cedula_electronica(ruta: Path) -> Documento | None:
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
            "contiene_pii": True,
            "fuente": ruta.name,
        },
    )


def _vetado(texto: str) -> str | None:
    plano = texto.upper()
    for marcador in _VETADOS:
        if marcador in plano:
            return marcador
    return None


def _clasificar(stem: str) -> tuple[str, int | None]:
    nombre = slug(stem)
    anio = None
    if (m := re.search(r"(19|20)\d{2}", nombre)):
        anio = int(m.group(0))
    if nombre.startswith("cv"):
        return "cv", anio
    if "certificado" in nombre or "constancia" in nombre:
        return "certificado", anio
    if "titulo" in nombre:
        return "titulo", anio
    return "documento", anio


def pdf_generico(ruta: Path, tipo: str | None = None) -> Documento | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("Falta pypdf: pip install pypdf", file=sys.stderr)
        raise SystemExit(2)

    paginas = [p.extract_text() or "" for p in PdfReader(ruta).pages]
    lineas = [l.rstrip() for pagina in paginas for l in pagina.splitlines()]
    limpias = [l for l in lineas if l.strip() and not _RUIDO.match(l.strip())]
    texto = "\n".join(limpias).strip()

    if (marcador := _vetado(texto)):
        raise VetadoError(marcador)
    if len(texto) < 200:
        return None  # sin capa de texto útil: necesita OCR o transcripción

    inferido, anio = _clasificar(ruta.stem)
    metadata = {"tipo": tipo or inferido, "contiene_pii": True, "fuente": ruta.name}
    if anio:
        metadata["anio"] = anio
    return Documento(
        nombre=f"{slug(ruta.stem)}.md",
        texto=f"# {ruta.stem}\n\n{texto}\n",
        metadata=metadata,
    )


class VetadoError(Exception):
    """El documento es de identidad y no puede entrar al corpus."""


def actividad_github(ruta: Path, *, incluir_repos: bool = False) -> Documento | None:
    """Perfil público de GitHub: actividad de desarrollo por año.

    Los nombres de repositorio privados quedan fuera salvo petición explícita:
    el agente puede acreditar volumen y constancia sin exponer en qué trabaja
    un cliente.
    """
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    usuario = datos.get("data", {}).get("user", datos)
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
            "contiene_pii": False,
            "fuente": ruta.name,
        },
    )


def transcripciones(carpeta: Path) -> list[Documento]:
    """Documentos transcritos a mano (plan E2, paso 3).

    Los certificados escaneados no tienen capa de texto. Con pocos documentos,
    transcribirlos es más rápido y más fiable que pelear con OCR — y el
    resultado es un fragmento limpio en vez de uno con errores de
    reconocimiento que el agente citaría como si fueran el original.
    """
    docs: list[Documento] = []
    if not carpeta.is_dir():
        return docs
    for md in sorted(carpeta.glob("*.md")):
        lateral = md.with_suffix(".md.metadata.json")
        metadata = {"tipo": "documento", "contiene_pii": True, "fuente": "transcripción manual"}
        if lateral.is_file():
            crudo = json.loads(lateral.read_text(encoding="utf-8"))
            metadata = crudo.get("metadataAttributes", crudo)
        docs.append(Documento(nombre=md.name, texto=md.read_text(encoding="utf-8"), metadata=metadata))
    return docs


def procesar(
    origen: Path, destino: Path, patrones: list[str], omitir: list[str] | None = None
) -> list[Documento]:
    documentos: list[Documento] = []
    archivos = sorted({a for patron in patrones for a in origen.glob(patron)})
    descartados = {a for patron in (omitir or []) for a in origen.glob(patron)}
    for archivo in archivos:
        if archivo in descartados:
            # Típicamente: el original de un documento ya transcrito a mano. Su
            # capa de texto residual solo duplicaría el fragmento limpio.
            print(f"  ↷ {archivo.name}: omitido (ya cubierto por transcripción manual)")
            continue
        if archivo.suffix.lower() == ".xml":
            doc = cedula_electronica(archivo)
        elif archivo.suffix.lower() == ".pdf":
            # Si existe el XML firmado del mismo documento, ese manda.
            if archivo.with_suffix(".xml").exists():
                print(f"  ↷ {archivo.name}: se usa su XML firmado en su lugar")
                continue
            try:
                doc = pdf_generico(archivo)
            except VetadoError as veto:
                print(f"  ⛔ {archivo.name}: contiene «{veto}» → documento de identidad, EXCLUIDO")
                continue
            if doc is None:
                print(f"  ⚠ {archivo.name}: sin capa de texto útil → requiere OCR o transcripción")
                continue
        elif archivo.suffix.lower() == ".json":
            doc = actividad_github(archivo)
        else:
            continue
        if doc:
            documentos.append(doc)
    return documentos


def escribir(documentos: list[Documento], destino: Path) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    for doc in documentos:
        (destino / doc.nombre).write_text(doc.texto, encoding="utf-8")
        (destino / f"{doc.nombre}.metadata.json").write_text(
            json.dumps({"metadataAttributes": doc.metadata}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  ✔ {doc.nombre}  ({len(doc.texto)} caracteres)")


def manifiesto(documentos: list[Documento], destino: Path) -> None:
    lineas = ["archivo,tipo,anio,institucion,contiene_pii,fuente"]
    for doc in documentos:
        m = doc.metadata
        lineas.append(
            f"{doc.nombre},{m.get('tipo','')},{m.get('anio','')},"
            f"\"{m.get('institucion','')}\",{str(m.get('contiene_pii', False)).lower()},{m.get('fuente','')}"
        )
    (destino / "manifiesto.csv").write_text("\n".join(lineas) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepara el corpus del agente")
    parser.add_argument("--source", required=True, help="Carpeta con los documentos de origen")
    parser.add_argument("--out", required=True, help="Carpeta destino (FUERA del repositorio)")
    parser.add_argument(
        "--only", nargs="*", default=["*.xml", "*.pdf", "*.json"],
        help="Patrones de archivo a procesar",
    )
    parser.add_argument(
        "--transcripciones", help="Carpeta con transcripciones manuales en Markdown"
    )
    parser.add_argument(
        "--skip", nargs="*", default=[], help="Patrones de archivo a omitir del origen"
    )
    args = parser.parse_args()

    origen = Path(args.source).expanduser().resolve()
    destino = Path(args.out).expanduser().resolve()

    if REPO in destino.parents or destino == REPO:
        print(f"❌ El destino {destino} está dentro del repositorio. Los documentos de identidad "
              f"no se guardan en git, ni siquiera ignorados.", file=sys.stderr)
        return 1
    if not origen.is_dir():
        print(f"❌ No existe la carpeta de origen: {origen}", file=sys.stderr)
        return 1

    print(f"Origen : {origen}\nDestino: {destino}\n")
    documentos = procesar(origen, destino, args.only, args.skip)
    if args.transcripciones:
        manuales = transcripciones(Path(args.transcripciones).expanduser().resolve())
        print(f"  + {len(manuales)} transcripción(es) manual(es)")
        documentos += manuales
    if not documentos:
        print("No se generó ningún documento.")
        return 1
    escribir(documentos, destino)
    manifiesto(documentos, destino)
    print(f"\n{len(documentos)} documento(s) listos. Para usarlos:\n"
          f"  export LUISCV_CORPUS_DIR={destino}\n  make run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
