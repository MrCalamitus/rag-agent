#!/usr/bin/env python3
"""Preparación del corpus: originales → fragmentos legibles + metadatos.

    python scripts/prep_corpus.py --profile coches
    python scripts/prep_corpus.py --profile luis-cv --source ~/docsLuis --out ~/docsLuis/corpus

La lógica vive en `rag_agent.infrastructure.ingest`: es la misma que usa el menú
interactivo, y así se prueba una sola vez. Este archivo es la línea de comandos
y nada más — resuelve el perfil, decide origen y destino, e informa.

Va documento por documento: cada uno se escribe y se reporta en cuanto termina,
sin esperar al lote. Un corpus de doscientos PDF con transcripción tarda horas,
y guardarlo entero en memoria para volcarlo al final tenía dos problemas —
ninguna señal de vida mientras corre, y un Ctrl-C o un fallo a la página 190 se
llevaba por delante las 189 conversiones que ya estaban hechas. Lo único que
espera al final es `manifiesto.csv`, cuya cabecera necesita las columnas de
todo el lote.

Sin `--source` / `--out` se toman los declarados en `profiles/<perfil>.yaml`,
que es el camino normal: el perfil ya sabe dónde están sus documentos.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from rag_agent.infrastructure.ingest import (  # noqa: E402
    DestinoInvalido,
    Escritor,
    Reporte,
    preparar_stream,
)
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

    reporte = Reporte()
    # El destino se valida y se abre ANTES de procesar nada: descubrir que la
    # carpeta estaba prohibida después de media hora de transcripciones sería
    # tirar el trabajo, y ahora el trabajo se va escribiendo según sale.
    escritor = None
    if not args.dry_run:
        try:
            escritor = Escritor(destino, binding.profile)
        except DestinoInvalido as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1

    vistos = _Novedades(reporte)
    for avance in preparar_stream(
        origen,
        binding.profile,
        reporte=reporte,
        patrones=tuple(args.only),
        omitir=tuple(args.skip),
        carpeta_cache=carpeta_cache,
        ocr=usar_ocr,
    ):
        if escritor is not None:
            escritor.anadir(avance.fragmentos)
        _imprimir(avance, escrito=escritor is not None)
        vistos.imprimir()

    if not reporte.total_fragmentos:
        print("\nNo se generó ningún fragmento.")
        return 1

    if args.dry_run:
        print(f"\n(dry-run) {reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s).")
        return 0

    # El manifiesto es lo único que sí necesita el lote completo: su cabecera
    # lleva todas las columnas que apareció en cualquier documento.
    escritor.cerrar()

    print(
        f"\n{reporte.documentos} documento(s) → {reporte.total_fragmentos} fragmento(s) en {destino}"
    )
    if reporte.errores or reporte.sin_texto or reporte.vetados or reporte.omitidos:
        print(
            f"Sin indexar: {len(reporte.errores)} ilegible(s), {len(reporte.sin_texto)} sin texto, "
            f"{len(reporte.vetados)} vetado(s), {len(reporte.omitidos)} omitido(s)."
        )
    if reporte.transcritos:
        print(f"{len(reporte.transcritos)} de ellos rescatados por transcripción.")
    print(f"Siguiente paso:  make sync-kb PROFILE={binding.slug}")
    return 0


SIMBOLOS = {
    "ok": "✔",
    "vetado": "⛔",
    "error": "✗",
    "sin_texto": "⚠",
    "omitido": "↷",
}


def _imprimir(avance, *, escrito: bool) -> None:
    """Una línea por documento, en cuanto ese documento está en disco.

    El ✔ significa «escrito»: en `--dry-run` no lo hay, porque marcar como
    hecho un archivo que nadie creó es exactamente lo que no debe pasar.
    """
    cabeza = f"  [{avance.indice}/{avance.total}] {avance.archivo.name}"
    if avance.estado == "ok":
        print(f"{cabeza}  → {len(avance.fragmentos)} fragmento(s)", flush=True)
        marca = "✔" if escrito else "·"
        for fragmento in avance.fragmentos:
            print(f"       {marca} {fragmento.nombre}  ({len(fragmento.texto)} caracteres)", flush=True)
        return
    detalle = avance.detalle
    if avance.estado == "vetado":
        detalle = f"contiene «{detalle}» → EXCLUIDO por el perfil"
    print(f"{cabeza}  {SIMBOLOS[avance.estado]} {detalle}", flush=True)


class _Novedades:
    """Imprime lo que el reporte fue acumulando desde el documento anterior.

    Las transcripciones y los avisos del OCR los anota el pipeline por su
    cuenta; enseñarlos aquí, entre documento y documento, es lo que convierte
    un lote de dos horas en algo que se puede mirar mientras corre.
    """

    def __init__(self, reporte) -> None:
        self._reporte = reporte
        self._transcritos = 0
        self._avisos = 0

    def imprimir(self) -> None:
        for nombre, motor, confianza in self._reporte.transcritos[self._transcritos:]:
            marca = f" (confianza {confianza}%)" if confianza is not None else ""
            print(f"       ⎋ {nombre}: transcrito con «{motor}»{marca}", flush=True)
        for aviso in self._reporte.avisos[self._avisos:]:
            print(f"       ⚠ {aviso}", flush=True)
        self._transcritos = len(self._reporte.transcritos)
        self._avisos = len(self._reporte.avisos)


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
