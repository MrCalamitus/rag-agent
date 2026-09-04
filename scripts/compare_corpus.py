#!/usr/bin/env python3
"""Compara dos corpus preparados y dice, con números, si conviene cambiar.

    python scripts/compare_corpus.py --a .corpus-preparado/autos \
                                     --b .corpus-preparado/autos-docling

Responde a las cuatro preguntas que decidían este experimento:

1. **Texto y fragmentos** — qué documentos gana y pierde cada lado, y cuánto
   texto sacan.
2. **Tablas** — dónde uno devuelve una tabla de Markdown y el otro texto corrido.
3. **Cobertura sin OCR** — qué documentos dejan de necesitar el motor de pago.
4. **Tiempo y costo** — segundos de conversión frente a dólares de Textract.

La comparación es **por documento original**, no por fragmento: los dos lados
trocean con la misma política pero sobre textos distintos, así que los
`document_id` con sufijo `--003` no se corresponden entre sí. El agregador es el
stem del documento, que sí es el mismo a ambos lados.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

from prep_corpus import USD_POR_1000_PAGINAS  # noqa: E402

_SUFIJO_FRAGMENTO = re.compile(r"--\d{3}$")
_FILA_TABLA = re.compile(r"^\s*\|.*\|\s*$")
_ESPACIOS = re.compile(r"[ \t]+")


@dataclass
class Doc:
    """Todos los fragmentos de un mismo documento original, recompuestos."""

    stem: str
    fragmentos: int = 0
    texto: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def caracteres(self) -> int:
        """Caracteres con los espacios colapsados, y por un motivo.

        Docling rellena las celdas de sus tablas Markdown hasta alinear las
        barras, y eso solo en la primera ficha medida inflaba el conteo de
        9.400 a 23.000: un 2,4x que era todo padding. Comparar en crudo daría
        siempre por ganador al lado que dibuja tablas, midiendo el dibujo y no
        el contenido.
        """
        return len(_ESPACIOS.sub(" ", self.texto))

    @property
    def filas_tabla(self) -> int:
        return sum(1 for l in self.texto.splitlines() if _FILA_TABLA.match(l))

    @property
    def bloques_tabla(self) -> int:
        bloques, dentro = 0, False
        for linea in self.texto.splitlines():
            if _FILA_TABLA.match(linea):
                if not dentro:
                    bloques += 1
                dentro = True
            else:
                dentro = False
        return bloques

    @property
    def origen(self) -> str:
        return str(self.metadata.get("origen_texto", "?"))


def leer(carpeta: Path) -> dict[str, Doc]:
    if not carpeta.is_dir():
        print(f"❌ No existe la carpeta: {carpeta}", file=sys.stderr)
        raise SystemExit(1)
    docs: dict[str, Doc] = {}
    for md in sorted(carpeta.glob("*.md")):
        stem = _SUFIJO_FRAGMENTO.sub("", md.stem)
        doc = docs.setdefault(stem, Doc(stem=stem))
        doc.fragmentos += 1
        # Los fragmentos solapan; concatenarlos infla el conteo de caracteres,
        # pero infla igual a los dos lados y sigue sirviendo para comparar.
        doc.texto += ("\n" if doc.texto else "") + md.read_text(encoding="utf-8")
        meta = md.with_suffix(".md.metadata.json")
        if meta.exists() and not doc.metadata:
            doc.metadata = json.loads(meta.read_text(encoding="utf-8")).get("metadataAttributes", {})
    return docs


def _tabla(filas: list[tuple[str, ...]], cabeceras: tuple[str, ...]) -> str:
    anchos = [max(len(str(f[i])) for f in [cabeceras, *filas]) for i in range(len(cabeceras))]
    def linea(f):
        return "  ".join(str(v).ljust(anchos[i]) for i, v in enumerate(f)).rstrip()
    return "\n".join([linea(cabeceras), "  ".join("-" * a for a in anchos), *(linea(f) for f in filas)])


def comparar(a: dict[str, Doc], b: dict[str, Doc], reporte_b: dict | None) -> dict:
    solo_a = sorted(set(a) - set(b))
    solo_b = sorted(set(b) - set(a))
    comunes = sorted(set(a) & set(b))

    print("═" * 72)
    print("1. TEXTO Y FRAGMENTOS")
    print("═" * 72)
    print(_tabla(
        [
            ("A", len(a), sum(d.fragmentos for d in a.values()), sum(d.caracteres for d in a.values())),
            ("B", len(b), sum(d.fragmentos for d in b.values()), sum(d.caracteres for d in b.values())),
        ],
        ("lado", "documentos", "fragmentos", "caracteres"),
    ))
    if solo_a:
        print(f"\n  Solo en A ({len(solo_a)}) — B los perdió:")
        for s in solo_a:
            print(f"    − {s}  ({a[s].caracteres} caracteres, origen «{a[s].origen}»)")
    if solo_b:
        print(f"\n  Solo en B ({len(solo_b)}) — B los rescató:")
        for s in solo_b:
            print(f"    + {s}  ({b[s].caracteres} caracteres, origen «{b[s].origen}»)")

    # Los documentos donde más cambió el volumen de texto son los que hay que
    # abrir a mano: o B recuperó una tabla entera, o se comió media página.
    deltas = [s for s in comunes if a[s].caracteres != b[s].caracteres]
    deltas.sort(key=lambda s: -abs(b[s].caracteres - a[s].caracteres))
    if deltas:
        print("\n  Mayor divergencia de texto (abrir estos primero):")
        filas = [
            (s, a[s].caracteres, b[s].caracteres, f"{b[s].caracteres - a[s].caracteres:+d}")
            for s in deltas[:10]
        ]
        print("    " + _tabla(filas, ("documento", "A", "B", "Δ")).replace("\n", "\n    "))

    print("\n" + "═" * 72)
    print("2. TABLAS")
    print("═" * 72)
    gana, pierde = [], []
    for s in comunes:
        da, db = a[s], b[s]
        if db.filas_tabla > da.filas_tabla:
            gana.append((s, da.filas_tabla, db.filas_tabla, db.bloques_tabla))
        elif da.filas_tabla > db.filas_tabla:
            pierde.append((s, da.filas_tabla, db.filas_tabla, da.bloques_tabla))
    print(f"  Filas de tabla Markdown — A: {sum(d.filas_tabla for d in a.values())}  "
          f"B: {sum(d.filas_tabla for d in b.values())}")
    if gana:
        print(f"\n  B estructura mejor ({len(gana)} documentos):")
        print("    " + _tabla(sorted(gana, key=lambda f: -(f[2] - f[1]))[:10],
                              ("documento", "filas A", "filas B", "bloques B")).replace("\n", "\n    "))
    if pierde:
        print(f"\n  A estructura mejor ({len(pierde)} documentos) — regresión a mirar:")
        print("    " + _tabla(sorted(pierde, key=lambda f: -(f[1] - f[2]))[:10],
                              ("documento", "filas A", "filas B", "bloques A")).replace("\n", "\n    "))
    if not gana and not pierde:
        print("  Ningún documento cambia su conteo de filas de tabla.")

    print("\n" + "═" * 72)
    print("3. COBERTURA SIN OCR DE PAGO")
    print("═" * 72)
    rescatados = [s for s in comunes if a[s].origen.startswith("ocr:") and not b[s].origen.startswith("ocr:")]
    rescatados += [s for s in solo_b if not b[s].origen.startswith("ocr:")]
    siguen = [s for s in comunes if b[s].origen.startswith("ocr:")]
    regresion = solo_a
    print(f"  Documentos que B resuelve sin OCR de pago y A no: {len(rescatados)}")
    for s in sorted(rescatados):
        print(f"    ✔ {s}  (A: «{a[s].origen if s in a else '—'}» → B: «{b[s].origen}»)")
    if siguen:
        print(f"\n  Siguen necesitando el motor externo en B: {len(siguen)}")
        for s in siguen:
            print(f"    · {s}  («{b[s].origen}»)")
    if regresion:
        print(f"\n  Regresión: {len(regresion)} documento(s) que A indexa y B deja fuera.")

    print("\n" + "═" * 72)
    print("4. TIEMPO Y COSTO")
    print("═" * 72)
    segundos = sum(float(d.metadata.get("docling_segundos", 0) or 0) for d in b.values())
    if reporte_b:
        segundos = sum(m.get("segundos", 0) for m in reporte_b.get("documentos", []))
    paginas_rescatadas = sum(int(a[s].metadata.get("paginas", 0) or 0) for s in rescatados if s in a)
    ahorro = paginas_rescatadas / 1000 * USD_POR_1000_PAGINAS
    print(f"  Conversión con Docling: {segundos:.1f}s en total"
          + (f" ({segundos / len(b):.1f}s por documento)" if b else ""))
    print(f"  Páginas que dejarían de ir a Textract: {paginas_rescatadas}")
    print(f"  Ahorro por pasada completa: ~{ahorro:.2f} USD "
          f"({USD_POR_1000_PAGINAS:.0f} USD/1.000 páginas)")
    print("  Nota: el caché de OCR ya evita repagar una pasada repetida; el ahorro real")
    print("  es el de la primera ingesta de cada documento nuevo o modificado.")

    return {
        "documentos": {"a": len(a), "b": len(b), "solo_a": solo_a, "solo_b": solo_b},
        "fragmentos": {"a": sum(d.fragmentos for d in a.values()), "b": sum(d.fragmentos for d in b.values())},
        "caracteres": {"a": sum(d.caracteres for d in a.values()), "b": sum(d.caracteres for d in b.values())},
        "filas_tabla": {"a": sum(d.filas_tabla for d in a.values()), "b": sum(d.filas_tabla for d in b.values())},
        "tablas_mejor_b": [g[0] for g in gana],
        "tablas_mejor_a": [p[0] for p in pierde],
        "rescatados_sin_ocr": sorted(rescatados),
        "siguen_con_ocr": siguen,
        "segundos_docling": round(segundos, 1),
        "paginas_textract_evitadas": paginas_rescatadas,
        "ahorro_usd": round(ahorro, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compara dos corpus preparados")
    parser.add_argument("--a", required=True, help="Corpus de referencia (el actual)")
    parser.add_argument("--b", required=True, help="Corpus a evaluar (el de Docling)")
    parser.add_argument("--diff", help="Imprime el diff completo de este document_id y termina")
    parser.add_argument("--json", dest="json_out", help="Guarda el resumen en este archivo")
    args = parser.parse_args()

    a = leer(Path(args.a).expanduser().resolve())
    b = leer(Path(args.b).expanduser().resolve())

    if args.diff:
        stem = _SUFIJO_FRAGMENTO.sub("", args.diff.removesuffix(".md"))
        if stem not in a or stem not in b:
            print(f"❌ '{stem}' no está en ambos corpus.", file=sys.stderr)
            return 1
        for linea in difflib.unified_diff(
            a[stem].texto.splitlines(), b[stem].texto.splitlines(),
            fromfile=f"A/{stem}", tofile=f"B/{stem}", lineterm="",
        ):
            print(linea)
        return 0

    reporte_b = None
    ruta_reporte = Path(args.b).expanduser().resolve() / "reporte-docling.json"
    if ruta_reporte.exists():
        reporte_b = json.loads(ruta_reporte.read_text(encoding="utf-8"))

    resumen = comparar(a, b, reporte_b)
    print("\nPara ver un documento entero:  "
          f"python scripts/compare_corpus.py --a {args.a} --b {args.b} --diff <document_id>")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Resumen guardado en {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
