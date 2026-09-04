#!/usr/bin/env python3
"""Genera fichas de índice del corpus preparado, ingestables en el KB.

    python scripts/generar_indice.py --profile finanzas
    python scripts/generar_indice.py --profile finanzas --force
    python scripts/generar_indice.py --profile finanzas --verificar

Produce en `.corpus-preparado/<slug>/`:
  - indice-<slug-doc>.md + .metadata.json  (una por documento fuente)
  - indice-corpus.md    + .metadata.json   (maestro por familia)

Cada ficha lleva un resumen redactado por un LLM leyendo fragmentos
representativos del documento. Es idempotente: guarda `hash_fuente` en la
metadata y salta las regeneraciones cuando los fragmentos no cambiaron.

Orden obligatorio en el pipeline del proyecto:
    make corpus  PROFILE=<slug>   # produce los fragmentos
    make indice  PROFILE=<slug>   # produce las fichas (este script)
    make sync-kb PROFILE=<slug>   # sube todo al KB de Bedrock

Los nombres `indice-*` no colisionan con `<slug>--NNN.md` (el separador `--` es
la firma inequívoca de fragmento) y `sync-kb.sh` los sube tal cual porque la
allowlist es `*.md` y `*.metadata.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from rag_agent.infrastructure.profiles import load_profiles  # noqa: E402

MODELO_POR_DEFECTO = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION_POR_DEFECTO = "us-east-1"

# Cuántos fragmentos leemos por documento para el resumen. Basta la portada,
# el índice y unos saltos: más contexto no mejora la síntesis y encarece el run.
FRAGMENTOS_MUESTRA = 8

# Paralelismo: lo empuja el límite de tokens/min del modelo, no la latencia.
CONCURRENCIA = 5

# Trunca cada fragmento a este tamaño antes de mandarlo al LLM. Un chunk suele
# rondar 1.8 KB y esto solo protege contra outliers.
MAX_CHARS_POR_FRAGMENTO = 2500

FAMILIAS = [
    (re.compile(r"^\d[tT]\d\d"), "reporte-trimestral", "Reporte trimestral"),
    (re.compile(r"^cnbv-"), "reporte-anual", "Reporte anual CNBV"),
    (re.compile(r"^reporte-anual-"), "reporte-anual", "Reporte anual"),
    (re.compile(r"^informe-anual-"), "reporte-anual", "Informe Anual Integrado"),
    (re.compile(r"^informe-anual-"), "reporte-anual", "Informe Anual Integrado"),
    (re.compile(r"^eeff-"), "estados-financieros", "Estados Financieros"),
    (re.compile(r"^.*-balance"), "balance-mensual", "Balance mensual"),
    (re.compile(r"^estado-de-situacin-"), "estado-situacion", "Estado de Situación Financiera"),
]

MESES_BALANCE = {"mzo": "Marzo", "jun": "Junio", "sep": "Septiembre", "dic": "Diciembre"}


def clasificar_familia(slug_doc: str) -> tuple[str, str]:
    for patron, familia, titulo_base in FAMILIAS:
        if patron.match(slug_doc):
            return familia, titulo_base
    return "documento", "Documento"


def periodo_desde_slug(slug_doc: str) -> str | None:
    """Infiere el periodo del slug cuando el patrón lo hace obvio."""
    m = re.match(r"^(\d)t(\d\d)", slug_doc)
    if m:
        return f"{m.group(1)}T{m.group(2)}"
    m = re.match(r"^(mzo|jun|sep|dic)(\d\d)-balance", slug_doc)
    if m:
        return f"{MESES_BALANCE[m.group(1)]} 20{m.group(2)}"
    m = re.match(r"^informe-anual-integrado-(\d{4})", slug_doc)
    if m:
        return m.group(1)
    m = re.match(r"^cnbv-n-(\d{4})", slug_doc)
    if m:
        return m.group(1)
    m = re.match(r"^reporte-anual-anexo-n-(\d{4})", slug_doc)
    if m:
        return m.group(1)
    m = re.match(r"^estado-de-situacin-financiera-(\d[tT]\d\d)", slug_doc)
    if m:
        return m.group(1).upper()
    m = re.match(r"^eeff-(\d{4})", slug_doc)
    if m:
        return m.group(1)
    return None


@dataclass
class Documento:
    slug: str
    fragmentos: list[Path]

    @property
    def total_fragmentos(self) -> int:
        return len(self.fragmentos)

    def muestra(self, n: int) -> list[Path]:
        """N fragmentos representativos: portada, índice, dos cortes medios y el final."""
        total = len(self.fragmentos)
        if total <= n:
            return list(self.fragmentos)
        n_primeros = max(1, n * 6 // 10)
        seleccion: list[Path] = list(self.fragmentos[:n_primeros])
        seleccion.append(self.fragmentos[total // 3])
        seleccion.append(self.fragmentos[2 * total // 3])
        seleccion.append(self.fragmentos[-1])
        vistos: set[Path] = set()
        out: list[Path] = []
        for p in seleccion:
            if p not in vistos:
                vistos.add(p)
                out.append(p)
        return out


def descubrir_documentos(carpeta: Path) -> dict[str, Documento]:
    """Agrupa `<slug>--NNN.md` por slug, ordenando NNN numéricamente."""
    grupos: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for ruta in carpeta.iterdir():
        if not ruta.is_file() or ruta.suffix != ".md":
            continue
        if ruta.name.startswith("indice-"):
            continue
        stem = ruta.stem
        if "--" not in stem:
            continue
        slug_doc, _, tail = stem.rpartition("--")
        try:
            n = int(tail)
        except ValueError:
            continue
        grupos[slug_doc].append((n, ruta))
    documentos: dict[str, Documento] = {}
    for slug_doc, pares in grupos.items():
        pares.sort(key=lambda p: p[0])
        documentos[slug_doc] = Documento(slug=slug_doc, fragmentos=[p for _, p in pares])
    return documentos


def leer_primero_metadata(doc: Documento) -> dict:
    """Metadata del fragmento 001 — trae fuente PDF, páginas, año si aplica."""
    lateral = doc.fragmentos[0].with_suffix(".md.metadata.json")
    if not lateral.is_file():
        return {}
    try:
        crudo = json.loads(lateral.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return crudo.get("metadataAttributes", {}) or {}


def hash_muestra(rutas: Iterable[Path]) -> str:
    """Hash estable de los bytes de los fragmentos usados en el prompt."""
    h = hashlib.sha256()
    for ruta in rutas:
        h.update(ruta.name.encode("utf-8"))
        h.update(b"\0")
        h.update(ruta.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


PROMPT_FICHA = """Eres un analista financiero. Vas a leer fragmentos representativos de un documento del corpus de reportes financieros de la emisora. Tu tarea: producir un resumen estructurado en JSON estricto que se usará como ficha de índice.

Devuelve EXCLUSIVAMENTE un objeto JSON con esta forma exacta, sin envolturas markdown:

{{
  "titulo": "string — título humano de 4 a 10 palabras, en español, empezando por 'la emisora' cuando aplique. Incluye el periodo si es obvio.",
  "resumen": "string — 2 a 4 líneas describiendo qué contiene el documento: alcance, foco temático, tipo de estados financieros incluidos, audiencia.",
  "secciones": ["string", "..."],
  "cifras": ["string", "..."]
}}

- "secciones": 3 a 6 secciones o capítulos identificados; en español; breves.
- "cifras": 0 a 5 cifras destacadas mencionadas, con formato 'Utilidad neta: 14,618 mdp (2T24)'. Si no ves cifras claras, deja [].

Contexto ya conocido (no lo repitas, úsalo como pista):
- Documento fuente: {fuente_pdf}
- Familia inferida: {familia}
- Periodo inferido: {periodo_hint}
- Páginas del PDF original: {paginas}
- Total de fragmentos indexados: {total_fragmentos}

Fragmentos representativos (portada, índice, secciones y cierre):
---
{fragmentos_texto}
---

Recuerda: SOLO el objeto JSON, sin ```json ni comentarios."""


PROMPT_MAESTRO = """Eres un editor técnico. Vas a redactar la introducción (2-3 líneas) de un archivo maestro que lista los {n_docs} documentos del corpus 'finanzas' del RAG.

Los documentos ya están clasificados por familia. Solo necesito una introducción SÍNTESIS breve del alcance temporal y temático de la colección.

Familias y conteos:
{familias_resumen}

Rango temporal (por periodos vistos): {rango_temporal}

Devuelve EXCLUSIVAMENTE un objeto JSON:

{{
  "intro": "string — 2 a 3 líneas describiendo la colección: emisor, alcance temporal, tipos de documento, para qué sirve como fuente al agente"
}}
"""


async def llamar_llm(client, modelo: str, prompt: str) -> str:
    """Bedrock Converse. Devuelve el texto de la respuesta."""

    def _call():
        return client.converse(
            modelId=modelo,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1500},
        )

    resp = await asyncio.to_thread(_call)
    for bloque in resp["output"]["message"]["content"]:
        if "text" in bloque:
            return bloque["text"]
    return ""


def extraer_json(texto: str) -> dict:
    """Parsea el texto como JSON. Tolera fences ```json y prosa alrededor."""
    limpio = texto.strip()
    if limpio.startswith("```"):
        lineas = limpio.splitlines()
        if lineas[0].startswith("```"):
            lineas = lineas[1:]
        if lineas and lineas[-1].startswith("```"):
            lineas = lineas[:-1]
        limpio = "\n".join(lineas).strip()
    inicio = limpio.find("{")
    if inicio == -1:
        raise ValueError(f"Sin JSON en respuesta: {texto[:200]!r}")
    profundidad = 0
    fin = -1
    for i, c in enumerate(limpio[inicio:], start=inicio):
        if c == "{":
            profundidad += 1
        elif c == "}":
            profundidad -= 1
            if profundidad == 0:
                fin = i + 1
                break
    if fin == -1:
        raise ValueError(f"JSON sin cerrar: {texto[:200]!r}")
    return json.loads(limpio[inicio:fin])


def renderizar_ficha(
    *,
    slug_doc: str,
    familia: str,
    periodo: str | None,
    fuente_pdf: str,
    paginas: int,
    total_fragmentos: int,
    titulo: str,
    resumen: str,
    secciones: list[str],
    cifras: list[str],
) -> str:
    padding = max(3, len(str(total_fragmentos)))
    ultimo = str(total_fragmentos).zfill(padding)
    cabecera_familia = f"**Familia**: {familia}"
    if periodo:
        cabecera_familia += f"  ·  **Periodo**: {periodo}"
    lineas = [
        f"# {titulo}",
        "",
        f"**Documento fuente**: `{fuente_pdf}`",
        cabecera_familia,
        f"**Páginas del PDF original**: {paginas}  ·  **Fragmentos indexados**: {total_fragmentos}",
        f"**Rango de fragmentos**: `{slug_doc}--001.md` … `{slug_doc}--{ultimo}.md`",
        "",
        "## Resumen",
        resumen.strip() or "(sin resumen)",
        "",
        "## Secciones clave",
    ]
    if secciones:
        lineas.extend(f"- {s}" for s in secciones)
    else:
        lineas.append("- (sin secciones extraíbles)")
    lineas.extend(["", "## Cifras destacadas"])
    if cifras:
        lineas.extend(f"- {c}" for c in cifras)
    else:
        lineas.append("- (sin cifras extraídas — consultar fragmentos)")
    lineas.extend([
        "",
        "## Cómo citar",
        f"Los datos concretos viven en los fragmentos `{slug_doc}--NNN.md`. Este archivo es un mapa; para dar una cifra específica cita el fragmento correspondiente.",
        "",
    ])
    return "\n".join(lineas)


def metadata_ficha(
    *,
    slug_doc: str,
    familia: str,
    periodo: str | None,
    fuente_pdf: str,
    paginas: int,
    total_fragmentos: int,
    hash_fuente: str,
    anio: int | None = None,
) -> dict:
    attrs: dict[str, object] = {
        "tipo": "indice",
        "clase": "publico",
        "fuente": fuente_pdf,
        "documento_slug": slug_doc,
        "familia": familia,
        "fragmentos_cubiertos": total_fragmentos,
        "paginas_originales": paginas,
        "hash_fuente": hash_fuente,
    }
    if periodo:
        attrs["periodo"] = periodo
    if anio is not None:
        attrs["anio"] = anio
    return {"metadataAttributes": attrs}


async def generar_una_ficha(
    doc: Documento,
    *,
    carpeta: Path,
    client,
    modelo: str,
    semaforo: asyncio.Semaphore,
    force: bool,
) -> dict:
    """Genera indice-<slug>.md + .metadata.json para un documento."""
    ruta_ficha = carpeta / f"indice-{doc.slug}.md"
    ruta_meta = carpeta / f"indice-{doc.slug}.md.metadata.json"

    meta_primer = leer_primero_metadata(doc)
    fuente_pdf = meta_primer.get("fuente") or f"{doc.slug}.pdf"
    paginas = int(meta_primer.get("paginas") or 0)
    anio_meta = meta_primer.get("anio")
    anio: int | None
    if isinstance(anio_meta, int):
        anio = anio_meta
    elif isinstance(anio_meta, str) and anio_meta.isdigit():
        anio = int(anio_meta)
    else:
        anio = None

    familia, titulo_base = clasificar_familia(doc.slug)
    periodo = periodo_desde_slug(doc.slug)

    muestra = doc.muestra(FRAGMENTOS_MUESTRA)
    hash_actual = hash_muestra(muestra)

    if not force and ruta_meta.is_file():
        try:
            existente = json.loads(ruta_meta.read_text(encoding="utf-8"))
            if existente.get("metadataAttributes", {}).get("hash_fuente") == hash_actual:
                print(f"↷ {doc.slug} (sin cambios, salto)")
                return existente
        except (json.JSONDecodeError, OSError):
            pass

    fragmentos_texto = "\n\n---\n\n".join(
        f"[{p.stem}]\n{p.read_text(encoding='utf-8')[:MAX_CHARS_POR_FRAGMENTO]}"
        for p in muestra
    )

    prompt = PROMPT_FICHA.format(
        fuente_pdf=fuente_pdf,
        familia=familia,
        periodo_hint=periodo or "(no inferido)",
        paginas=paginas or "(desconocido)",
        total_fragmentos=doc.total_fragmentos,
        fragmentos_texto=fragmentos_texto,
    )

    async with semaforo:
        print(f"→ {doc.slug} (llamando LLM con {len(muestra)} fragmentos)")
        respuesta = await llamar_llm(client, modelo, prompt)

    try:
        datos = extraer_json(respuesta)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"✗ {doc.slug}: respuesta no parseable — {exc}", file=sys.stderr)
        datos = {
            "titulo": f"{titulo_base} {periodo or doc.slug}",
            "resumen": f"Documento del corpus finanzas. Fuente: {fuente_pdf}.",
            "secciones": [],
            "cifras": [],
        }

    ficha_md = renderizar_ficha(
        slug_doc=doc.slug,
        familia=familia,
        periodo=periodo,
        fuente_pdf=fuente_pdf,
        paginas=paginas,
        total_fragmentos=doc.total_fragmentos,
        titulo=datos.get("titulo") or f"{titulo_base} {periodo or doc.slug}",
        resumen=datos.get("resumen") or "",
        secciones=[str(s) for s in (datos.get("secciones") or []) if s],
        cifras=[str(c) for c in (datos.get("cifras") or []) if c],
    )
    meta = metadata_ficha(
        slug_doc=doc.slug,
        familia=familia,
        periodo=periodo,
        fuente_pdf=fuente_pdf,
        paginas=paginas,
        total_fragmentos=doc.total_fragmentos,
        hash_fuente=hash_actual,
        anio=anio,
    )
    ruta_ficha.write_text(ficha_md, encoding="utf-8")
    ruta_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ {doc.slug}")
    return meta


def renderizar_maestro(
    *,
    metadatas: list[dict],
    intro: str,
    total_fragmentos: int,
) -> str:
    grupos: dict[str, list[dict]] = defaultdict(list)
    for m in metadatas:
        attrs = m.get("metadataAttributes", {})
        grupos[attrs.get("familia", "documento")].append(attrs)

    orden_familias = [
        ("reporte-trimestral", "Reportes trimestrales"),
        ("reporte-anual", "Reportes anuales y regulatorios"),
        ("estados-financieros", "Estados financieros anuales"),
        ("balance-mensual", "Balances mensuales"),
        ("estado-situacion", "Estado de situación financiera"),
        ("documento", "Otros"),
    ]

    lineas = [
        "# Índice del corpus financiero",
        "",
        intro.strip(),
        "",
        f"Total: {len(metadatas)} documentos, {total_fragmentos} fragmentos indexados.",
        "",
        "Cada línea remite a una ficha `indice-<slug>.md` con el resumen del documento.",
        "",
    ]
    for familia, titulo in orden_familias:
        docs = grupos.get(familia, [])
        if not docs:
            continue
        docs.sort(key=lambda a: (str(a.get("periodo") or ""), str(a.get("fuente") or "")))
        lineas.append(f"## {titulo} ({len(docs)})")
        lineas.append("")
        for a in docs:
            slug_doc = a["documento_slug"]
            periodo = a.get("periodo") or ""
            fuente = a.get("fuente") or f"{slug_doc}.pdf"
            frags = a.get("fragmentos_cubiertos", "?")
            paginas = a.get("paginas_originales")
            paginas_txt = f", {paginas} págs" if paginas else ""
            etiqueta = f"**{periodo}**" if periodo else f"**{slug_doc}**"
            lineas.append(
                f"- {etiqueta} — `{fuente}` — {frags} fragmentos{paginas_txt} — [ficha](indice-{slug_doc}.md)"
            )
        lineas.append("")
    lineas.extend([
        "---",
        "",
        "**Nota para el agente**: si te preguntan '¿qué documentos hay?' o '¿qué reportes tienes?', cita este archivo. Para dar una cifra concreta, cita el fragmento correspondiente `<slug>--NNN.md`.",
        "",
    ])
    return "\n".join(lineas)


async def generar_maestro(
    metadatas: list[dict],
    *,
    carpeta: Path,
    client,
    modelo: str,
    total_fragmentos: int,
) -> None:
    ruta_maestro = carpeta / "indice-corpus.md"
    ruta_meta = carpeta / "indice-corpus.md.metadata.json"

    grupos: dict[str, int] = defaultdict(int)
    anios: set[int] = set()
    trimestres: list[str] = []
    for m in metadatas:
        a = m.get("metadataAttributes", {})
        grupos[a.get("familia", "documento")] += 1
        periodo = str(a.get("periodo") or "")
        # 1T24, 2T25, etc. — extrae año 2 dígitos
        m_trim = re.match(r"^([1-4])T(\d\d)$", periodo)
        if m_trim:
            trimestres.append(periodo)
            anios.add(2000 + int(m_trim.group(2)))
        elif re.match(r"^(19|20)\d{2}$", periodo):
            anios.add(int(periodo))
        else:
            m_mes = re.match(r"^\w+ (\d{4})$", periodo)
            if m_mes:
                anios.add(int(m_mes.group(1)))

    if anios:
        rango = f"años {min(anios)} a {max(anios)}"
        if trimestres:
            def _clave(t: str) -> tuple[int, int]:
                return (int(t[2:4]), int(t[0]))  # (año, trimestre)
            trims_ord = sorted(trimestres, key=_clave)
            rango += f" (trimestres desde {trims_ord[0]} hasta {trims_ord[-1]})"
    else:
        rango = "(sin periodos etiquetados)"
    familias_resumen = "\n".join(f"- {k}: {v}" for k, v in grupos.items())

    prompt = PROMPT_MAESTRO.format(
        n_docs=len(metadatas),
        familias_resumen=familias_resumen,
        rango_temporal=rango,
    )
    print("→ maestro (llamando LLM para intro)")
    try:
        respuesta = await llamar_llm(client, modelo, prompt)
        datos = extraer_json(respuesta)
        intro = str(datos.get("intro") or "").strip()
    except Exception as exc:  # noqa: BLE001 - frontera con boto3/LLM
        print(f"⚠ maestro: no se pudo generar intro con LLM ({exc}); uso fallback", file=sys.stderr)
        intro = (
            "Colección de reportes financieros públicos de la emisora "
            "(reportes trimestrales, informes anuales integrados, estados financieros "
            "y balances mensuales)."
        )

    contenido = renderizar_maestro(
        metadatas=metadatas,
        intro=intro,
        total_fragmentos=total_fragmentos,
    )

    if ruta_maestro.is_file() and ruta_maestro.read_text(encoding="utf-8") == contenido:
        print("↷ maestro (sin cambios, salto)")
    else:
        ruta_maestro.write_text(contenido, encoding="utf-8")
        print("✓ maestro")

    meta = {
        "metadataAttributes": {
            "tipo": "indice",
            "clase": "publico",
            "fuente": "corpus-finanzas",
            "familia": "indice-maestro",
            "documentos_cubiertos": len(metadatas),
            "fragmentos_cubiertos": total_fragmentos,
        }
    }
    ruta_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def verificar(carpeta: Path) -> int:
    """Chequeos de smoke sobre indice-*. Devuelve 0 si todo bien, 1 si hay error."""
    problemas: list[str] = []

    fichas = sorted(carpeta.glob("indice-*.md"))
    if not fichas:
        print("✗ Sin fichas de índice en la carpeta", file=sys.stderr)
        return 1

    for ficha in fichas:
        if "--" in ficha.stem:
            problemas.append(f"colisión con fragmento: {ficha.name}")
        meta = ficha.with_suffix(".md.metadata.json")
        if not meta.is_file():
            problemas.append(f"sin metadata: {ficha.name}")
            continue
        try:
            datos = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problemas.append(f"metadata JSON inválido: {meta.name} — {exc}")
            continue
        if "metadataAttributes" not in datos:
            problemas.append(f"metadata sin metadataAttributes: {meta.name}")
        tam = ficha.stat().st_size
        if not (400 < tam < 8000):
            problemas.append(f"{ficha.name}: {tam} bytes fuera de rango 400-8000")

    print(f"→ {len(fichas)} fichas encontradas")

    try:
        from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import (
            LocalCorpusKnowledgeBase,
        )
    except ImportError as exc:
        print(f"⚠ no se pudo importar LocalCorpusKnowledgeBase ({exc}); salto smoke", file=sys.stderr)
    else:
        kb = LocalCorpusKnowledgeBase(carpeta)
        outcome = await kb.retrieve(["qué documentos tienes", "índice del corpus"], top_k=5)
        top_ids = [c.document_id for c in outcome.chunks]
        print("→ top-5 para 'qué documentos tienes / índice del corpus':")
        for cid in top_ids:
            print(f"    {cid}")
        if not any(cid.startswith("indice-") for cid in top_ids):
            problemas.append(f"ninguna ficha de índice aparece en top-5: {top_ids}")

    if problemas:
        print("\n✗ Problemas detectados:", file=sys.stderr)
        for p in problemas:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("\n✓ Verificación OK")
    return 0


async def main_async(args: argparse.Namespace) -> int:
    perfiles = load_profiles(RAIZ / "profiles")
    if args.profile not in perfiles:
        print(f"✗ Perfil desconocido: {args.profile}. Disponibles: {sorted(perfiles)}", file=sys.stderr)
        return 1

    binding = perfiles[args.profile]
    prep = binding.prepared_dir
    if not prep:
        print(f"✗ Perfil {args.profile} no declara corpus.prepared", file=sys.stderr)
        return 1

    prep_path = Path(prep).expanduser()
    carpeta = prep_path.resolve() if prep_path.is_absolute() else (RAIZ / prep_path).resolve()
    if not carpeta.is_dir():
        print(f"✗ Carpeta no existe: {carpeta}", file=sys.stderr)
        return 1

    if args.verificar:
        return await verificar(carpeta)

    documentos = descubrir_documentos(carpeta)
    if not documentos:
        print(f"✗ Sin fragmentos '<slug>--NNN.md' en {carpeta}", file=sys.stderr)
        return 1

    total_fragmentos = sum(d.total_fragmentos for d in documentos.values())
    print(f"→ {len(documentos)} documentos, {total_fragmentos} fragmentos totales en {carpeta}")

    import boto3

    kwargs: dict[str, object] = {"region_name": args.region}
    if args.aws_profile:
        kwargs["profile_name"] = args.aws_profile
    session = boto3.Session(**kwargs)
    client = session.client("bedrock-runtime")

    semaforo = asyncio.Semaphore(CONCURRENCIA)
    tareas = [
        generar_una_ficha(
            doc, carpeta=carpeta, client=client, modelo=args.model,
            semaforo=semaforo, force=args.force,
        )
        for doc in documentos.values()
    ]
    metadatas = list(await asyncio.gather(*tareas))

    await generar_maestro(
        metadatas,
        carpeta=carpeta, client=client, modelo=args.model,
        total_fragmentos=total_fragmentos,
    )

    print(f"\n✓ {len(documentos)} fichas + 1 maestro escritos en {carpeta}")
    if args.verificar_al_final:
        return await verificar(carpeta)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("--profile", required=True, help="Slug del perfil (profiles/<slug>.yaml)")
    parser.add_argument("--model", default=MODELO_POR_DEFECTO,
                        help=f"ID del modelo Bedrock (default: {MODELO_POR_DEFECTO})")
    parser.add_argument("--region", default=REGION_POR_DEFECTO)
    parser.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE"),
                        help="Perfil AWS a usar (default: env AWS_PROFILE)")
    parser.add_argument("--force", action="store_true",
                        help="Regenera todas las fichas ignorando hash")
    parser.add_argument("--verificar", action="store_true",
                        help="Solo corre chequeos, no genera")
    parser.add_argument("--verificar-al-final", action="store_true",
                        help="Corre chequeos después de generar")
    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
