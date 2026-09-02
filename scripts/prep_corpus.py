#!/usr/bin/env python3
"""Preparación del corpus: originales → fragmentos legibles + metadatos.

    python scripts/prep_corpus.py --profile coches
    python scripts/prep_corpus.py --profile luis-cv --source ~/docsLuis --out ~/docsLuis/corpus

La lógica vive en `rag_agent.infrastructure.ingest`: es la misma que usa el menú
interactivo, y así se prueba una sola vez. Este archivo es la línea de comandos
y nada más — resuelve el perfil, decide origen y destino, e imprime el reporte.

Sin `--source` / `--out` se toman los declarados en `profiles/<perfil>.yaml`,
que es el camino normal: el perfil ya sabe dónde están sus documentos.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from rag_agent.infrastructure.ingest import DestinoInvalido, escribir, preparar  # noqa: E402
from rag_agent.infrastructure.ingest.extractors import EXTENSIONES  # noqa: E402
from rag_agent.infrastructure.ingest.pipeline import escanear_ocr  # noqa: E402
from rag_agent.infrastructure.profiles import ProfileError, load_profiles  # noqa: E402

# Precio de lista de Textract AnalyzeDocument (TABLES) por 1.000 páginas en
# us-east-1 cuando se escribió esto. Está aquí y no escondido para que se pueda
# corregir de un vistazo: lo que no cambia es que conviene ver el número de
# páginas ANTES de lanzar el lote, no a mitad.
USD_POR_1000_PAGINAS = 15.0


def _ruta(valor: str | None) -> Path | None:
    return Path(valor).expanduser().resolve() if valor else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepara el corpus de un perfil")
    parser.add_argument("--profile", required=True, help="Slug del perfil (profiles/<slug>.yaml)")
    parser.add_argument("--profiles-dir", default=str(RAIZ / "profiles"))
    parser.add_argument("--source", help="Carpeta de originales (por defecto: la del perfil)")
    parser.add_argument("--out", help="Carpeta destino (por defecto: la del perfil)")
    parser.add_argument(
        "--only", nargs="*", default=[f"*{ext}" for ext in EXTENSIONES],
        help="Patrones de archivo a procesar",
    )
    parser.add_argument("--skip", nargs="*", default=[], help="Patrones a omitir del origen")
    parser.add_argument("--dry-run", action="store_true", help="Analiza sin escribir nada")
    parser.add_argument(
        "--no-ocr", action="store_true",
        help="No transcribir los PDF sin capa de texto (se reportan y se omiten)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="No preguntar antes de transcribir",
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
    destino = _ruta(args.out) or _ruta(binding.prepared_dir)
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

    print(f"Perfil : {binding.profile.name} ({binding.slug})")
    print(f"Origen : {origen}")
    print(f"Destino: {destino}\n")

    # El caché vive junto al corpus preparado y no dentro del origen: la carpeta
    # de documentos es del usuario y no debe llenarse de archivos nuestros.
    carpeta_cache = destino.parent / ".ocr-cache"
    usar_ocr = not args.no_ocr and binding.profile.ocr.activo
    if usar_ocr and not _confirmar_ocr(origen, binding, carpeta_cache, asumir_si=args.yes or args.dry_run):
        usar_ocr = False

    reporte = preparar(
        origen,
        binding.profile,
        patrones=tuple(args.only),
        omitir=tuple(args.skip),
        carpeta_cache=carpeta_cache,
        ocr=usar_ocr,
    )

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

    if not reporte.fragmentos:
        print("\nNo se generó ningún fragmento.")
        return 1

    if args.dry_run:
        print(f"\n(dry-run) {reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s).")
        return 0

    try:
        escribir(reporte, destino, binding.profile)
    except DestinoInvalido as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    for fragmento in reporte.fragmentos:
        print(f"  ✔ {fragmento.nombre}  ({len(fragmento.texto)} caracteres)")
    print(
        f"\n{reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s) en {destino}"
    )
    if reporte.transcritos:
        print(f"{len(reporte.transcritos)} de ellos rescatados por transcripción.")
    print(f"Siguiente paso:  make sync-kb PROFILE={binding.slug}")
    return 0


def _confirmar_ocr(origen, binding, carpeta_cache, *, asumir_si: bool) -> bool:
    """Enseña qué se va a transcribir y cuánto cuesta, y pide permiso.

    El motor en la nube cobra por página. Enterarse del gasto a mitad de un lote
    de cien no sirve de nada, y un `make corpus` no debería poder sorprender a
    nadie con una factura.
    """
    candidatos = escanear_ocr(origen, binding.profile, carpeta_cache)
    if not candidatos:
        return True

    pendientes = [c for c in candidatos if not c.en_cache]
    cacheados = len(candidatos) - len(pendientes)
    paginas = sum(c.paginas for c in pendientes)

    print(f"  {len(candidatos)} PDF sin capa de texto:")
    for c in candidatos[:8]:
        estado = "en caché" if c.en_cache else f"{c.paginas} pág."
        print(f"    · {c.ruta.name}  ({estado})")
    if len(candidatos) > 8:
        print(f"    · … y {len(candidatos) - 8} más")
    if cacheados:
        print(f"  {cacheados} ya transcritos antes: no se vuelven a procesar ni a pagar.")
    if not pendientes:
        print()
        return True

    print(f"\n  A transcribir: {len(pendientes)} documento(s), {paginas} página(s), "
          f"motor «{binding.profile.ocr.motor}».")
    if binding.profile.ocr.motor == "tablas":
        print(f"  Costo estimado: ~{paginas / 1000 * USD_POR_1000_PAGINAS:.2f} USD "
              f"(Textract, {USD_POR_1000_PAGINAS:.0f} USD/1.000 páginas).")
    if asumir_si:
        print()
        return True
    try:
        respuesta = input("  ¿Transcribir? [S/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    print()
    return respuesta in ("", "s", "si", "sí", "y", "yes")


if __name__ == "__main__":
    raise SystemExit(main())
