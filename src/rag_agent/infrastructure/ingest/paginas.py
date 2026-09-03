"""Qué páginas de un PDF necesitan extracción con estructura.

El supuesto que traía la ingesta —«necesita motor» ⇔ «no tiene capa de texto»—
funciona con folletos escaneados y falla con un informe financiero nativo: su
capa de texto está completa, pero `pypdf` la devuelve aplanada. Una tabla de
resultados sale como una columna de cifras sin fila ni encabezado, y esa forma
es peor que no tener el dato: parece texto legítimo y se indexa como tal.

Este módulo decide, por página, si hace falta el motor de tablas. No decide
*cuánto se gasta*: decide *qué preserva el significado*.

Medido sobre un informe financiero trimestral de 85 páginas: 84 llevan tabla. Por eso
el modo recomendado para ese perfil es `todas` — seleccionar no ahorra nada y
cualquier fallo del detector pierde una tabla en silencio. `con-tablas` existe
para corpus mayoritariamente narrativos, donde sí hay algo que ahorrar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Un path tan fino es una regla de tabla, no una figura.
GROSOR_MAX_REGLA = 2.5
# Una regla más corta que esto es un subrayado o un adorno.
LARGO_MIN_REGLA = 0.08
# Por debajo de esto la página no tiene texto propio: es un escaneo.
MIN_CHARS_PAGINA = 120
# Con tan poco texto y tanta imagen, lo que hay es una página rasterizada.
MAX_COBERTURA_IMAGEN = 0.60
# Área mínima para que una imagen cuente como figura y no como logotipo.
MIN_AREA_FIGURA = 0.01

PDFIUM_PATH = 2
PDFIUM_IMAGEN = 3


@dataclass(frozen=True)
class Figura:
    """Una imagen incrustada, en coordenadas normalizadas estilo Textract."""

    pagina: int
    left: float
    top: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class SenalesPagina:
    numero: int
    caracteres: int
    cobertura_imagen: float
    reglas_h: int
    figuras: tuple[Figura, ...] = ()

    @property
    def escaneada(self) -> bool:
        """Sin capa de texto propia.

        Las dos condiciones juntas a propósito: mirar solo el número de
        caracteres marcaría como escaneo una portada legítimamente escueta.
        """
        return (
            self.caracteres < MIN_CHARS_PAGINA
            and self.cobertura_imagen > MAX_COBERTURA_IMAGEN
        )


def senales(ruta: Path) -> list[SenalesPagina]:
    """Señales de cada página. Solo pypdfium2, ya en las dependencias."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - depende del entorno
        from .ocr.rasterize import falta_dependencia

        raise SystemExit(falta_dependencia("pypdfium2")) from None

    documento = pdfium.PdfDocument(str(ruta))
    try:
        return [_senales_pagina(documento[i], i + 1) for i in range(len(documento))]
    finally:
        documento.close()


def _senales_pagina(pagina, numero: int) -> SenalesPagina:
    ancho, alto = pagina.get_width(), pagina.get_height()
    area = max(ancho * alto, 1.0)

    cobertura = 0.0
    reglas_h = 0
    figuras: list[Figura] = []

    for objeto in pagina.get_objects(max_depth=15):
        # get_bounds() ya viene transformado por la matriz del objeto; PdfImage
        # no expone get_pos(), que es el accesor que uno esperaría.
        if objeto.type == PDFIUM_IMAGEN:
            izq, abajo, der, arriba = objeto.get_bounds()
            w, h = abs(der - izq) / ancho, abs(arriba - abajo) / alto
            cobertura = max(cobertura, w * h)
            if w * h >= MIN_AREA_FIGURA:
                figuras.append(
                    Figura(
                        pagina=numero,
                        left=izq / ancho,
                        # pdfium mide desde abajo y Textract desde arriba: sin
                        # este giro los recortes salen espejados en vertical.
                        top=1.0 - (arriba / alto),
                        width=w,
                        height=h,
                    )
                )
        elif objeto.type == PDFIUM_PATH:
            izq, abajo, der, arriba = objeto.get_bounds()
            if abs(arriba - abajo) < GROSOR_MAX_REGLA and abs(der - izq) > ancho * LARGO_MIN_REGLA:
                reglas_h += 1

    return SenalesPagina(
        numero=numero,
        caracteres=len(pagina.get_textpage().get_text_range()),
        cobertura_imagen=cobertura,
        reglas_h=reglas_h,
        figuras=tuple(sorted(figuras, key=lambda f: (f.top, f.left))),
    )


def seleccionar(ruta: Path, modo: str, *, total: int) -> set[int]:
    """Páginas que deben pasar por el motor de tablas, 1-indexadas."""
    if modo == "todas":
        return set(range(1, total + 1))
    if modo != "con-tablas":
        return set()

    marcadas = {s.numero for s in senales(ruta) if s.escaneada or s.reglas_h >= 1}
    # Una tabla sin rejilla solo se ve por la alineación de sus columnas, y eso
    # exige un analizador de layout. Sin la extra instalada se manda todo: es
    # preferible pagar de más a perder una tabla sin enterarse.
    detectadas = _paginas_con_tabla_por_layout(ruta)
    if detectadas is None:
        return set(range(1, total + 1))
    return marcadas | detectadas


def _paginas_con_tabla_por_layout(ruta: Path) -> set[int] | None:
    """Páginas con tabla según pdfplumber, o `None` si no está instalado.

    `pdfplumber` es una extra y no una dependencia: solo el modo `con-tablas`
    la necesita, y arrastra `pdfminer.six`, que vuelve a parsear el documento
    entero. Devolver `None` en vez de fallar deja que quien no la instaló siga
    preparando su corpus.
    """
    try:
        import pdfplumber
    except ImportError:
        return None

    lineas = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    texto = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "min_words_vertical": 3,
        "min_words_horizontal": 2,
    }

    encontradas: set[int] = set()
    with pdfplumber.open(str(ruta)) as documento:
        for indice, pagina in enumerate(documento.pages, start=1):
            for ajustes in (lineas, texto):
                try:
                    tablas = pagina.find_tables(table_settings=ajustes)
                except Exception:  # noqa: BLE001 - pdfminer falla de mil formas
                    continue
                if any(_sustantiva(t) for t in tablas):
                    encontradas.add(indice)
                    break
    return encontradas


def _sustantiva(tabla) -> bool:
    """Descarta candidatas degeneradas.

    Sin este filtro la estrategia por texto llama tabla a tres líneas de datos
    de contacto en una portada: tres filas de una sola columna.
    """
    filas = tabla.rows
    if not filas:
        return False
    columnas = max((len(f.cells) for f in filas), default=0)
    celdas = sum(1 for f in filas for c in f.cells if c)
    return columnas >= 2 and celdas >= 6
