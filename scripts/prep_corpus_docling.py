#!/usr/bin/env python3
"""Igual que `prep_corpus.py`, pero extrayendo los PDF con Docling.

    python scripts/prep_corpus_docling.py --profile autos
    python scripts/prep_corpus_docling.py --profile autos --docling-ocr

Escribe en `<destino del perfil>-docling`, nunca encima del corpus bueno, para
poder comparar los dos lado a lado:

    python scripts/compare_corpus.py --a .corpus-preparado/autos \
                                     --b .corpus-preparado/autos-docling

El transcriptor externo (Textract, tesseract) viene **apagado** por defecto: la
pregunta que este banco de pruebas responde es cuánto resuelve Docling solo.
`--docling-ocr` enciende el OCR interno de Docling, que es local y gratis;
`--textract-fallback` vuelve a permitir el de pago para el caso mixto.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "scripts"))

from lab import docling_extractor  # noqa: E402
from rag_agent.infrastructure.ingest import (  # noqa: E402
    DestinoInvalido,
    Reporte,
    escribir,
    preparar,
)
from rag_agent.infrastructure.ingest.extractors import EXTENSIONES  # noqa: E402
from rag_agent.infrastructure.profiles import ProfileError, load_profiles  # noqa: E402


def _ruta(valor: str | None) -> Path | None:
    return Path(valor).expanduser().resolve() if valor else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepara el corpus de un perfil con Docling")
    parser.add_argument("--profile", required=True, help="Slug del perfil (profiles/<slug>.yaml)")
    parser.add_argument("--profiles-dir", default=str(RAIZ / "profiles"))
    parser.add_argument("--source", help="Carpeta de originales (por defecto: la del perfil)")
    parser.add_argument("--out", help="Carpeta destino (por defecto: la del perfil + '-docling')")
    parser.add_argument(
        "--only", nargs="*", default=[f"*{ext}" for ext in EXTENSIONES],
        help="Patrones de archivo a procesar",
    )
    parser.add_argument("--skip", nargs="*", default=[], help="Patrones a omitir del origen")
    parser.add_argument("--dry-run", action="store_true", help="Analiza sin escribir nada")
    parser.add_argument(
        "--docling-ocr", action="store_true",
        help="Enciende el OCR interno de Docling (local, sin costo) para los PDF escaneados",
    )
    parser.add_argument(
        "--textract-fallback", action="store_true",
        help="Permite además el transcriptor de pago del perfil cuando Docling no saca texto",
    )
    args = parser.parse_args()

    try:
        perfiles = load_profiles(args.profiles_dir)
    except ProfileError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    binding = perfiles.get(args.profile)
    if binding is None:
        disponibles = ", ".join(sorted(perfiles)) or "(ninguno)"
        print(f"❌ No existe el perfil '{args.profile}'. Disponibles: {disponibles}", file=sys.stderr)
        return 1

    origen = _ruta(args.source) or _ruta(binding.source_dir)
    base_destino = _ruta(binding.prepared_dir)
    destino = _ruta(args.out) or (base_destino.with_name(base_destino.name + "-docling") if base_destino else None)
    if origen is None or destino is None:
        print(
            f"❌ El perfil '{args.profile}' no declara carpetas de corpus. "
            f"Pásalas con --source y --out, o añádelas a su YAML.",
            file=sys.stderr,
        )
        return 1
    if not origen.is_dir():
        print(f"❌ No existe la carpeta de origen: {origen}", file=sys.stderr)
        return 1

    modo = "docling + OCR local" if args.docling_ocr else "docling (sin OCR)"
    print(f"Perfil : {binding.profile.name} ({binding.slug})")
    print(f"Origen : {origen}")
    print(f"Destino: {destino}")
    print(f"Extraer: {modo}\n")

    docling_extractor.instalar(do_ocr=args.docling_ocr)

    archivos = _archivos(origen, tuple(args.only))
    if not archivos:
        print("❌ Ningún archivo coincide con --only.", file=sys.stderr)
        return 1

    reporte = Reporte()
    for indice, archivo in enumerate(archivos, start=1):
        print(f"  [{indice}/{len(archivos)}] {archivo.name}", end="", flush=True)
        parcial = preparar(
            origen,
            binding.profile,
            # Un documento por llamada. `preparar` recorre la carpeta entera y
            # `escribir` vuelca al final, que con pypdf eran segundos y aquí son
            # media hora sin una sola señal de vida. Acotarlo a un archivo
            # convierte el mismo código en un pipeline incremental sin tocarlo.
            patrones=(archivo.relative_to(origen).as_posix(),),
            omitir=tuple(args.skip),
            carpeta_cache=destino.parent / ".ocr-cache",
            ocr=args.textract_fallback,
        )
        _acumular(reporte, parcial)
        medida = docling_extractor.MEDICIONES.get(str(archivo), {})
        segundos = medida.get("segundos")
        detalle = f" {segundos}s" if segundos is not None else ""
        if parcial.fragmentos and not args.dry_run:
            try:
                escribir(parcial, destino, binding.profile)
            except DestinoInvalido as exc:
                print(f"\n❌ {exc}", file=sys.stderr)
                return 1
        estado = (
            f"{len(parcial.fragmentos)} fragmento(s)" if parcial.fragmentos else "sin texto"
        )
        print(f"{detalle}  → {estado}", flush=True)
    print()

    for nombre, marcador in reporte.vetados:
        print(f"  ⛔ {nombre}: contiene «{marcador}» → EXCLUIDO por el perfil")
    for nombre, motor, confianza in reporte.transcritos:
        marca = f" (confianza {confianza}%)" if confianza is not None else ""
        print(f"  ⎋ {nombre}: transcrito con «{motor}»{marca}")
    for aviso in reporte.avisos:
        print(f"  ⚠ {aviso}")
    for nombre, detalle in reporte.errores:
        print(f"  ✗ {nombre}: no se pudo leer → {detalle}")
    for nombre, motivo in reporte.sin_texto:
        print(f"  ⚠ {nombre}: {motivo}")
    for nombre, motivo in reporte.omitidos:
        print(f"  ↷ {nombre}: {motivo}")

    _resumen_docling()

    if not reporte.fragmentos:
        print("\nNo se generó ningún fragmento.")
        return 1

    if args.dry_run:
        print(f"\n(dry-run) {reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s).")
        return 0

    # Los .md ya están en disco desde su documento. Esta pasada solo rehace el
    # manifiesto, que necesita el lote completo para tener todas sus columnas.
    try:
        escribir(reporte, destino, binding.profile)
    except DestinoInvalido as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    # La materia prima del comparador: lo que cada conversión costó, incluidos
    # los documentos que luego se descartaron por quedarse sin texto.
    (destino / "reporte-docling.json").write_text(
        json.dumps(
            {
                "modo": modo,
                "textract_fallback": args.textract_fallback,
                "documentos": sorted(docling_extractor.MEDICIONES.values(), key=lambda m: m["archivo"]),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for fragmento in reporte.fragmentos:
        print(f"  ✔ {fragmento.nombre}  ({len(fragmento.texto)} caracteres)")
    print(
        f"\n{reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s) en {destino}"
    )
    print(f"Siguiente paso:  python scripts/compare_corpus.py --a {base_destino or '<corpus actual>'} --b {destino}")
    return 0


def _archivos(origen: Path, patrones: tuple[str, ...]) -> list[Path]:
    """Los archivos a procesar, en el mismo orden que usaría `preparar`."""
    encontrados = {a for patron in patrones for a in origen.rglob(patron) if a.is_file()}
    return sorted(encontrados)


def _acumular(total: Reporte, parcial: Reporte) -> None:
    """Suma el reporte de un documento al del lote."""
    total.fragmentos.extend(parcial.fragmentos)
    total.documentos += parcial.documentos
    for campo in ("vetados", "sin_texto", "omitidos", "errores", "transcritos", "avisos"):
        getattr(total, campo).extend(getattr(parcial, campo))


def _resumen_docling() -> None:
    """Lo que costó la conversión, que es la mitad de la decisión."""
    mediciones = list(docling_extractor.MEDICIONES.values())
    if not mediciones:
        return
    segundos = sum(m["segundos"] for m in mediciones)
    paginas = sum(m["paginas"] or 0 for m in mediciones)
    tablas = sum(m["tablas"] for m in mediciones)
    print(
        f"\n  Docling: {len(mediciones)} PDF, {paginas} página(s), {tablas} tabla(s) detectada(s), "
        f"{segundos:.1f}s en total ({segundos / len(mediciones):.1f}s por documento)."
    )
    lentos = sorted(mediciones, key=lambda m: -m["segundos"])[:3]
    for m in lentos:
        print(f"    · {m['archivo']}: {m['segundos']}s, {m['tablas']} tabla(s), {m['caracteres']} caracteres")


if __name__ == "__main__":
    raise SystemExit(main())
