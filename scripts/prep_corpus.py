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
from rag_agent.infrastructure.profiles import ProfileError, load_profiles  # noqa: E402


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

    reporte = preparar(origen, binding.profile, patrones=tuple(args.only), omitir=tuple(args.skip))

    for nombre, marcador in reporte.vetados:
        print(f"  ⛔ {nombre}: contiene «{marcador}» → EXCLUIDO por el perfil")
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
        f"\n{reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s) en {destino}\n"
        f"Siguiente paso:  make sync-kb PROFILE={binding.slug}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
