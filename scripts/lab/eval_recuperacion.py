#!/usr/bin/env python3
"""¿Con qué corpus puede el agente responder, y no solo encontrar algo?

    python scripts/lab/eval_recuperacion.py --generar corpus-docling --out preguntas.json
    python scripts/lab/eval_recuperacion.py --a corpus-a --b corpus-b --preguntas preguntas.json

La pregunta que decide si vale la pena cambiar el extractor no es cuánto texto
sale, sino si el fragmento recuperado **permite responder**. En una ficha
multi-versión eso se parte en dos cosas distintas, y medirlas juntas engaña:

1. **Recuperación** — ¿el fragmento contiene la característica? Es lo que
   cualquier corpus con el texto dentro hace bien, y donde los dos deberían
   empatar.
2. **Atribución** — ¿ese mismo fragmento nombra la versión? Sin eso el agente
   lee «Quemacocos - • • • •» y no puede decir si la I SPORT lo trae. El
   fragmento se encontró y aun así la pregunta se queda sin responder.

El banco de preguntas se deriva de las tablas de un corpus con estructura,
porque es el único sitio del que se puede leer la verdad —qué versión trae qué—
sin inventarla. Eso favorece al lado estructurado en la métrica 2 **por
construcción**, y no es un sesgo del experimento: es exactamente la capacidad
que se está comprando. La métrica 1 queda como control neutral.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "src"))

from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import (  # noqa: E402
    LocalCorpusKnowledgeBase,
)

_SEPARADOR = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# Una versión es un nombre corto y en mayúsculas: «I SPORT», «TROPHY», «TRD OFF
# ROAD». Sirve para no confundir la fila de versiones con una de datos.
_VERSION = re.compile(r"^[A-ZÁÉÍÓÚÑ0-9][A-ZÁÉÍÓÚÑ0-9 .\-+®/]{1,28}$")
_MARCA_SI = "•"
_MARCA_NO = {"-", "–", "—", ""}


def _plano(texto: str) -> str:
    d = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _celdas(fila: str) -> list[str]:
    return [c.strip() for c in fila.strip().strip("|").split("|")]


def generar(corpus: Path, maximo: int) -> list[dict]:
    """Saca preguntas discriminantes de las tablas: equipo que unas versiones
    traen y otras no. Una fila donde todas las versiones coinciden no distingue
    nada y no sirve para medir."""
    preguntas: list[dict] = []
    for md in sorted(corpus.glob("*.md")):
        lineas = md.read_text(encoding="utf-8").splitlines()
        versiones: list[str] = []
        for i, linea in enumerate(lineas):
            celdas = _celdas(linea)
            if _SEPARADOR.match(linea) or len(celdas) < 3:
                continue
            # ¿Es la fila que nombra las columnas?
            candidatas = celdas[1:]
            if all(_VERSION.match(c) for c in candidatas if c) and sum(bool(c) for c in candidatas) >= 2:
                if not any(m in c for c in candidatas for m in ("•", "-")):
                    versiones = candidatas
                    continue
            if not versiones or len(celdas) - 1 != len(versiones):
                continue
            etiqueta = celdas[0]
            valores = celdas[1:]
            if len(etiqueta) < 12 or etiqueta.isupper():
                continue
            trae = [v for v, m in zip(versiones, valores) if m == _MARCA_SI and v]
            nada = [v for v, m in zip(versiones, valores) if m in _MARCA_NO and v]
            if not trae or not nada:
                continue  # no discrimina: todas iguales
            modelo = md.stem.split("--")[0]
            preguntas.append({
                "documento": modelo,
                "version": trae[0],
                "caracteristica": etiqueta,
                "pregunta": f"{modelo.replace('-', ' ')} {trae[0]} {etiqueta}",
                "respuesta": True,
            })
    # Una por documento y característica, repartidas.
    vistos: set[tuple[str, str]] = set()
    unicas = []
    for p in preguntas:
        clave = (p["documento"], p["caracteristica"])
        if clave in vistos:
            continue
        vistos.add(clave)
        unicas.append(p)
    paso = max(1, len(unicas) // maximo)
    return unicas[::paso][:maximo]


async def evaluar(corpus: Path, preguntas: list[dict], top_k: int) -> dict:
    kb = LocalCorpusKnowledgeBase(corpus)
    encontrada = atribuible = 0
    halladas: set[str] = set()
    fallos: list[str] = []
    for p in preguntas:
        outcome = await kb.retrieve([p["pregunta"]], top_k=top_k)
        carac = _plano(p["caracteristica"])
        version = _plano(p["version"])
        hit = None
        for chunk in outcome.chunks:
            if carac in _plano(chunk.text):
                hit = chunk
                break
        if hit is None:
            fallos.append(f"no recuperada: {p['caracteristica'][:40]} ({p['documento']})")
            continue
        encontrada += 1
        halladas.add(p["pregunta"])
        if _alineable(hit.text, p["caracteristica"], p["version"]):
            atribuible += 1
        else:
            fallos.append(f"sin alinear «{p['version']}»: {p['caracteristica'][:40]}")
    total = len(preguntas)
    return {
        "total": total,
        "encontrada": encontrada,
        "atribuible": atribuible,
        "halladas": halladas,
        "pct_encontrada": round(100 * encontrada / total, 1) if total else 0.0,
        "pct_atribuible": round(100 * atribuible / total, 1) if total else 0.0,
        "fallos": fallos,
    }


def _alineable(texto: str, caracteristica: str, version: str) -> bool:
    """¿Se puede llevar la marca de esta fila hasta su columna?

    No basta con que el nombre de la versión aparezca en el fragmento. En el
    corpus plano aparece —suelto, en una línea de rótulos que sobró del PDF—
    y no sirve de nada: sigue sin haber forma de saber cuál de los cuatro
    puntos de «Escape doble cromado - - - •» es el de la SE. Cuenta solo si
    hay una cabecera que nombre la versión y una fila con la característica
    **en la misma tabla y con las mismas columnas**, que es lo que permite
    contar celdas y responder.
    """
    objetivo, quiere = _plano(caracteristica), _plano(version)
    cabecera: list[str] | None = None
    for linea in texto.splitlines():
        if not linea.lstrip().startswith("|"):
            continue
        if _SEPARADOR.match(linea):
            continue
        celdas = _celdas(linea)
        if any(quiere == _plano(c) for c in celdas[1:]):
            cabecera = celdas
            continue
        if cabecera and objetivo in _plano(celdas[0]) and len(celdas) == len(cabecera):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Eficacia de recuperación de dos corpus")
    parser.add_argument("--generar", help="Corpus con tablas del que sacar el banco de preguntas")
    parser.add_argument("--out", help="Dónde escribir el banco generado")
    parser.add_argument("--maximo", type=int, default=60, help="Preguntas a generar")
    parser.add_argument("--a", help="Corpus de referencia")
    parser.add_argument("--b", help="Corpus a evaluar")
    parser.add_argument("--preguntas", help="Banco de preguntas (JSON)")
    parser.add_argument("--top-k", type=int, default=6)
    args = parser.parse_args()

    if args.generar:
        preguntas = generar(Path(args.generar).expanduser().resolve(), args.maximo)
        destino = Path(args.out or "preguntas.json")
        destino.write_text(json.dumps(preguntas, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(preguntas)} pregunta(s) en {destino}")
        for p in preguntas[:5]:
            print(f"  · {p['pregunta']}")
        return 0

    if not (args.a and args.b and args.preguntas):
        parser.error("hacen falta --a, --b y --preguntas (o --generar)")

    preguntas = json.loads(Path(args.preguntas).read_text(encoding="utf-8"))
    resultados = {}
    for etiqueta, ruta in (("A", args.a), ("B", args.b)):
        resultados[etiqueta] = asyncio.run(
            evaluar(Path(ruta).expanduser().resolve(), preguntas, args.top_k)
        )

    print(f"{len(preguntas)} preguntas, top_k={args.top_k}\n")
    print(f"{'':22} {'A':>12} {'B':>12}")
    print("-" * 48)
    for clave, titulo in (
        ("pct_encontrada", "Característica hallada"),
        ("pct_atribuible", "Versión atribuible"),
    ):
        a, b = resultados["A"][clave], resultados["B"][clave]
        print(f"{titulo:22} {a:>11.1f}% {b:>11.1f}%")
    comunes = resultados["A"]["halladas"] & resultados["B"]["halladas"]
    if comunes:
        print(f"\n  Sobre las {len(comunes)} preguntas que AMBOS recuperan —el banco sale de las")
        print("  tablas de B, así que la etiqueta exacta le favorece; esto quita ese sesgo:")
        subset = [p for p in preguntas if p["pregunta"] in comunes]
        for etiqueta, ruta in (("A", args.a), ("B", args.b)):
            r = asyncio.run(evaluar(Path(ruta).expanduser().resolve(), subset, args.top_k))
            print(f"    {etiqueta}: {r['atribuible']}/{r['total']} atribuibles ({r['pct_atribuible']}%)")

    print()
    for etiqueta in ("A", "B"):
        r = resultados[etiqueta]
        print(f"  {etiqueta}: {r['encontrada']}/{r['total']} halladas, "
              f"{r['atribuible']}/{r['total']} atribuibles")
    print("\n  Ejemplos de fallo en A:")
    for f in resultados["A"]["fallos"][:5]:
        print(f"    · {f}")
    print("  Ejemplos de fallo en B:")
    for f in resultados["B"]["fallos"][:5] or ["(ninguno)"]:
        print(f"    · {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
