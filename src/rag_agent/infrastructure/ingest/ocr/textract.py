"""Extracción con estructura de tabla (AWS Textract).

Existe por un caso concreto y muy común en corpus técnicos: la **ficha
comparativa**. Filas de características, columnas de versiones, y en cada celda
un valor o una viñeta.

```
                        COOL MT   STYLE MT   STYLE CVT   EXCITE CVT
Potencia máxima neta            108 HP @ 4,500 rpm
Quemacocos eléctrico                                          •
```

Aplanada a texto corrido, esa tabla afirma que el modelo lleva quemacocos. Lo
lleva **una** de sus cuatro versiones. El agente lo citaría con toda propiedad y
estaría diciendo algo falso, que es justo lo que este proyecto existe para
evitar. Por eso la extracción conserva la rejilla y cada fila se emite ya
resuelta: qué valor corresponde a qué columna.

Dos cosas que Textract no resuelve solo y se resuelven aquí:

1. **Las viñetas no son texto.** El «•» de una ficha es un adorno tipográfico,
   no una casilla de selección, así que Textract devuelve la celda vacía. Se
   detectan midiendo la tinta dentro del recuadro que el propio Textract da para
   cada celda: una celda marcada tiene una mancha oscura pequeña, una vacía no
   tiene ninguna. La separación medida es limpia (≈0.005 frente a 0.000).
2. **Un valor centrado se parte entre columnas.** «108 HP @ 4,500 rpm», escrito
   una sola vez en mitad de la tabla, vuelve troceado en dos celdas contiguas.
   Se reagrupa por adyacencia y se decide a quién pertenece por geometría.

Sobre esa geometría, un detalle que costó una iteración: los recuadros que
Textract da para una CELL son los de la **rejilla**, no los del texto, y las
celdas contiguas se tocan. Agrupar por hueco entre celdas no separa nada. Los
huecos hay que medirlos entre las **palabras**, y ahí la separación es enorme:
sobre una ficha real, 0.002 dentro de un mismo valor frente a 0.19 entre dos
valores distintos.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from . import PaginaExtraida, ResultadoOcr

# Las operaciones síncronas de Textract tienen un límite de transacciones por
# segundo bajo. Un corpus de cien páginas lo supera enseguida, y sin reintentos
# la mitad del lote vuelve vacía. El modo adaptativo reduce el ritmo solo cuando
# el servicio lo pide, en vez de dormir a ciegas entre páginas.
REINTENTOS = {"max_attempts": 8, "mode": "adaptive"}
LECTURA_S = 60

# Fracción de píxeles oscuros dentro de una celda a partir de la cual *puede*
# haber una marca. Es condición necesaria, no suficiente: el umbral real se
# calibra por tabla (`_umbral_de_marca`), porque una banda fija afinada sobre un
# corpus lee como marca cualquier icono o sombreado de otro.
TINTA_MARCA_MIN = 0.0015
# Por encima de esto no es una viñeta sino un fondo de color o una imagen; una
# celda así no afirma nada y no debe leerse como marcada.
TINTA_MARCA_MAX = 0.35
# Margen que se recorta de la celda antes de medir, para no contar los bordes
# de la tabla como si fueran tinta.
MARGEN_CELDA = 0.18
# Separación máxima entre dos palabras para considerarlas el mismo valor, como
# fracción del ancho de la página. Medido: 0.002 dentro de un valor, 0.19 entre
# valores distintos. El umbral está en mitad de un hueco de dos órdenes de
# magnitud, no en el filo de nada.
HUECO_MISMO_VALOR = 0.01

# Hueco mínimo entre el grupo de celdas vacías y el de celdas marcadas para
# fiarse de la distinción. Sobre fichas reales el hueco medido es de 0.005; si
# en un documento no aparece esa separación limpia, no hay forma de saber qué es
# una marca y qué es un adorno, y no se emite ninguna.
SEPARACION_BIMODAL = 0.001

# Una viñeta es una mancha pequeña y centrada. Un sombreado de fondo o un icono
# ancho no lo son, y confundirlos afirma una característica que el documento no
# afirma — el error en la dirección peligrosa.
MANCHA_ANCHO_MAX = 0.55
MANCHA_ALTO_MAX = 0.75
MANCHA_DESVIO_MAX = 0.30

# Forma de tabla: fracción mínima de filas con etiqueta en la primera columna, y
# de celdas de valor vacías o marcadas, para reconocer una matriz comparativa.
# Una tabla de datos —un balance, un histórico— tiene casi todas las celdas
# llenas y no pasa estos filtros, que es justo lo que se busca.
MATRIZ_ETIQUETAS_MIN = 0.7
MATRIZ_VACIAS_MIN = 0.5

# Textract marca como TABLE cualquier maquetación en rejilla, incluida una plana
# de folleto con tres bloques de texto en fila. Tomar la primera fila de eso como
# encabezados empareja cosas que no van juntas —«1030 km de autonomía: 3 filas de
# asientos»— y ese emparejamiento es una afirmación falsa, no solo un formato
# feo. Un encabezado de verdad es una etiqueta corta.
CABECERA_LARGO_MAX = 30
CABECERA_FILAS_MIN = 3


@dataclass(frozen=True)
class Mancha:
    """Lo que hay dibujado en una celda sin texto."""

    fraccion: float = 0.0
    compacta: bool = False

    @property
    def posible_marca(self) -> bool:
        return self.compacta and TINTA_MARCA_MIN < self.fraccion < TINTA_MARCA_MAX


@dataclass(frozen=True)
class Celda:
    """Una celda con sus dos geometrías, que no son la misma.

    `rejilla_*` es el recuadro de la tabla —sirve para saber a qué columna
    pertenece y para medir la tinta de una viñeta—; `texto_*` es la extensión
    real de las palabras, que es lo único que permite ver dónde acaba un valor
    y empieza el siguiente.
    """

    fila: int
    columna: int
    texto: str
    rejilla_izq: float
    rejilla_der: float
    texto_izq: float = 0.0
    texto_der: float = 0.0
    mancha: Mancha = Mancha()

    @property
    def centro_rejilla(self) -> float:
        return (self.rejilla_izq + self.rejilla_der) / 2

    def marcada(self, umbral: float | None) -> bool:
        return (
            umbral is not None
            and not self.texto
            and self.mancha.compacta
            and self.mancha.fraccion >= umbral
        )


class TextractOcr:
    nombre = "tablas"

    def __init__(
        self,
        *,
        dpi: int = 200,
        region: str | None = None,
        profile: str | None = None,
        columnas: str = "columnas",
    ) -> None:
        self._dpi = dpi
        self._columnas = columnas
        self._region = region
        self._profile = profile
        self._cliente: Any | None = None

    # -- protocolo ------------------------------------------------------
    def disponible(self) -> tuple[bool, str]:
        try:
            import boto3  # noqa: F401
        except ImportError:  # pragma: no cover
            return False, "falta boto3"
        try:
            from PIL import Image  # noqa: F401
        except ImportError:  # pragma: no cover
            return False, 'falta Pillow: pip install -e ".[ingest]"'
        try:
            self._boto().describe_document_classifier  # atributo cualquiera: fuerza la sesión
        except AttributeError:
            pass
        except Exception as exc:  # noqa: BLE001 - credenciales o región
            return False, f"no se pudo crear el cliente de Textract: {exc}"
        return True, ""

    def paginas_por_documento(self, total: int) -> int:
        return total

    def extraer(self, imagenes: list[bytes], *, idioma: str = "spa") -> ResultadoOcr:
        resultado = ResultadoOcr(motor=self.nombre)
        for numero, imagen in enumerate(imagenes, start=1):
            try:
                respuesta = self._analizar(imagen)
            except Exception as exc:  # noqa: BLE001 - una página mala no tumba el documento
                resultado.avisos.append(f"página {numero}: {type(exc).__name__}: {exc}")
                continue
            resultado.paginas.append(_pagina(respuesta, imagen, numero, self._columnas))
        return resultado

    # -- interno --------------------------------------------------------
    def _boto(self) -> Any:
        if self._cliente is None:
            import boto3

            from botocore.config import Config

            sesion = boto3.Session(profile_name=self._profile, region_name=self._region)
            self._cliente = sesion.client(
                "textract",
                config=Config(connect_timeout=5, read_timeout=LECTURA_S, retries=REINTENTOS),
            )
        return self._cliente

    def _analizar(self, imagen: bytes) -> dict:
        return self._boto().analyze_document(
            Document={"Bytes": imagen}, FeatureTypes=["TABLES"]
        )


# --- traducción de la respuesta a texto ---------------------------------------


def _pagina(
    respuesta: dict, imagen: bytes, numero: int, columnas: str = "columnas"
) -> PaginaExtraida:
    bloques = {b["Id"]: b for b in respuesta.get("Blocks", [])}
    tinta = _medidor_de_tinta(imagen)

    tablas = [b for b in respuesta["Blocks"] if b["BlockType"] == "TABLE"]
    ids_en_tabla = {
        nieto
        for tabla in tablas
        for celda in _hijos(tabla, bloques, "CELL")
        for nieto in _ids_hijos(celda)
    }

    piezas: list[str] = []
    # Lo que no pertenece a ninguna tabla —títulos, notas al pie— va primero y
    # en su orden de lectura: da el contexto que la tabla no lleva dentro.
    for bloque in respuesta["Blocks"]:
        if bloque["BlockType"] == "LINE" and not (set(_ids_hijos(bloque)) & ids_en_tabla):
            piezas.append(bloque.get("Text", ""))
    for tabla in tablas:
        piezas.append(_render_tabla(tabla, bloques, tinta, columnas))

    confianzas = [
        b["Confidence"] for b in respuesta["Blocks"]
        if b["BlockType"] == "WORD" and "Confidence" in b
    ]
    return PaginaExtraida(
        numero=numero,
        texto="\n".join(p for p in piezas if p.strip()),
        confianza=round(sum(confianzas) / len(confianzas), 1) if confianzas else None,
        tablas=len(tablas),
    )


def _ids_hijos(bloque: dict) -> list[str]:
    return [
        hijo
        for rel in bloque.get("Relationships", [])
        if rel["Type"] == "CHILD"
        for hijo in rel["Ids"]
    ]


def _hijos(bloque: dict, bloques: dict, tipo: str) -> list[dict]:
    return [bloques[i] for i in _ids_hijos(bloque) if bloques.get(i, {}).get("BlockType") == tipo]


def _contenido(celda: dict, bloques: dict) -> tuple[str, float, float]:
    """Texto de la celda y la extensión horizontal real de sus palabras."""
    palabras = _hijos(celda, bloques, "WORD")
    piezas = [p["Text"] for p in palabras]
    for hijo in _hijos(celda, bloques, "SELECTION_ELEMENT"):
        if hijo.get("SelectionStatus") == "SELECTED":
            piezas.append("sí")
    if not palabras:
        return " ".join(piezas).strip(), 0.0, 0.0
    cajas = [p["Geometry"]["BoundingBox"] for p in palabras]
    return (
        " ".join(piezas).strip(),
        min(c["Left"] for c in cajas),
        max(c["Left"] + c["Width"] for c in cajas),
    )


def _medidor_de_tinta(imagen: bytes):
    """Devuelve una función recuadro → `Mancha` (cuánta tinta y con qué forma).

    La fracción sola no distingue una viñeta de un sombreado ni de un icono. Se
    mide también el recuadro que ocupan los píxeles oscuros: una viñeta es una
    mancha pequeña y centrada, y cualquier otra cosa no debe leerse como marca.
    """
    try:
        import io

        from PIL import Image
    except ImportError:  # pragma: no cover
        return lambda _: Mancha()

    gris = Image.open(io.BytesIO(imagen)).convert("L")
    ancho, alto = gris.size

    def medir(caja: dict) -> Mancha:
        x0 = caja["Left"] * ancho
        y0 = caja["Top"] * alto
        dx = caja["Width"] * ancho
        dy = caja["Height"] * alto
        # Se recorta un margen para no medir los bordes de la tabla.
        x0, dx = x0 + dx * MARGEN_CELDA, dx * (1 - 2 * MARGEN_CELDA)
        y0, dy = y0 + dy * MARGEN_CELDA, dy * (1 - 2 * MARGEN_CELDA)
        if dx < 3 or dy < 3:
            return Mancha()
        recorte = gris.crop((int(x0), int(y0), int(x0 + dx), int(y0 + dy)))
        w, h = recorte.size
        pixeles = recorte.tobytes()
        if not pixeles or w == 0:
            return Mancha()

        oscuros = 0
        min_x, max_x, min_y, max_y = w, -1, h, -1
        for indice, valor in enumerate(pixeles):
            if valor >= 128:
                continue
            oscuros += 1
            x, y = indice % w, indice // w
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        if not oscuros:
            return Mancha()

        ancho_rel = (max_x - min_x + 1) / w
        alto_rel = (max_y - min_y + 1) / h
        desvio_x = abs((min_x + max_x) / 2 / w - 0.5)
        desvio_y = abs((min_y + max_y) / 2 / h - 0.5)
        compacta = (
            ancho_rel <= MANCHA_ANCHO_MAX
            and alto_rel <= MANCHA_ALTO_MAX
            and desvio_x <= MANCHA_DESVIO_MAX
            and desvio_y <= MANCHA_DESVIO_MAX
        )
        return Mancha(fraccion=oscuros / len(pixeles), compacta=compacta)

    return medir


def _umbral_de_marca(manchas: list[Mancha]) -> float | None:
    """Umbral calibrado sobre esta tabla, o `None` si no se puede distinguir.

    Se busca la separación bimodal: un grupo de celdas casi sin tinta y otro con
    una mancha clara, con un hueco ancho entre ambos. Si el documento no la
    presenta —todas las celdas sombreadas, o ninguna marcada— no hay forma de
    saber qué es una viñeta, y entonces no se emite ninguna. Antes se daba por
    hecha una banda fija afinada sobre un único corpus.
    """
    candidatas = sorted(m.fraccion for m in manchas if m.fraccion <= TINTA_MARCA_MAX)
    if len(candidatas) < 2:
        return None
    hueco, corte = max(
        (b - a, (a + b) / 2) for a, b in zip(candidatas, candidatas[1:])
    )
    if hueco < SEPARACION_BIMODAL:
        return None
    bajas = [f for f in candidatas if f < corte]
    altas = [f for f in candidatas if f >= corte]
    if not bajas or not altas:
        return None
    # El grupo bajo tiene que ser realmente «vacío» y el alto realmente «marcado».
    if max(bajas) > TINTA_MARCA_MIN or min(altas) < TINTA_MARCA_MIN:
        return None
    return corte


def _es_matriz_comparativa(
    por_fila: dict, cabeceras: dict[int, str], umbral: float | None
) -> bool:
    """¿Es esta tabla una ficha comparativa, con filas de característica y
    columnas de versión?

    Solo con esa forma confirmada tiene sentido redactar «solo en X» o «vale
    para todas»: son afirmaciones sobre el significado de la rejilla. Aplicarlas
    a un balance o a un histórico de rendimientos inventaría un sentido que la
    tabla no tiene, y el error saldría en forma de frase perfectamente redactada.
    """
    if len(cabeceras) < 2:
        return False
    filas = sorted(por_fila)[1:]
    if not filas:
        return False

    con_etiqueta = sum(1 for f in filas if por_fila[f].get(1) and por_fila[f][1].texto)
    if con_etiqueta / len(filas) < MATRIZ_ETIQUETAS_MIN:
        return False

    valores = [c for f in filas for col, c in por_fila[f].items() if col > 1]
    if not valores:
        return False
    vacias = sum(1 for c in valores if not c.texto)
    return vacias / len(valores) >= MATRIZ_VACIAS_MIN


def _render_tabla(tabla: dict, bloques: dict, tinta, columnas: str = "columnas") -> str:
    celdas: list[Celda] = []
    for celda in _hijos(tabla, bloques, "CELL"):
        caja = celda["Geometry"]["BoundingBox"]
        texto, izq, der = _contenido(celda, bloques)
        celdas.append(
            Celda(
                fila=celda["RowIndex"],
                columna=celda["ColumnIndex"],
                texto=texto,
                rejilla_izq=caja["Left"],
                rejilla_der=caja["Left"] + caja["Width"],
                texto_izq=izq,
                texto_der=der,
                mancha=Mancha() if texto else tinta(caja),
            )
        )
    if not celdas:
        return ""

    por_fila: dict[int, dict[int, Celda]] = defaultdict(dict)
    for celda in celdas:
        por_fila[celda.fila][celda.columna] = celda

    filas = sorted(por_fila)
    cabecera = por_fila[filas[0]]
    cabeceras = _cabeceras(cabecera)
    # Centros tomados de la rejilla y no de las palabras: el centro de una
    # columna no debe moverse porque su encabezado sea más largo.
    centros = {c: celda.centro_rejilla for c, celda in cabecera.items() if c > 1}
    umbral = _umbral_de_marca([c.mancha for c in celdas if not c.texto])

    # La tabla decide cómo se lee. Solo una matriz comparativa admite que se
    # redacte su significado; cualquier otra forma se vuelca sin interpretar.
    if _es_matriz_comparativa(por_fila, cabeceras, umbral):
        render = lambda fila: _render_fila(fila, cabeceras, centros, columnas, umbral)  # noqa: E731
    else:
        todas = (
            _cabeceras(cabecera, incluir_primera=True)
            if _parece_cabecera(cabecera, len(filas))
            else {}
        )
        render = lambda fila: _render_fila_generica(fila, todas, umbral)  # noqa: E731

    lineas: list[str] = []
    for indice in filas[1:]:
        linea = render(por_fila[indice])
        if linea:
            lineas.append(linea)
    return "\n".join(lineas)


def _cabeceras(primera_fila: dict[int, Celda], *, incluir_primera: bool = False) -> dict[int, str]:
    """Columna → su encabezado.

    En una matriz comparativa la primera columna no es una columna de datos sino
    la etiqueta de la fila, y se excluye. En el volcado genérico sí cuenta, que
    es lo que hace que cada fila se explique sola.
    """
    return {
        columna: celda.texto
        for columna, celda in primera_fila.items()
        if celda.texto and (incluir_primera or columna > 1)
    }


def _parece_cabecera(primera_fila: dict[int, Celda], filas_totales: int) -> bool:
    """¿La primera fila son encabezados de columna, o es contenido?

    Si no lo son, el volcado genérico se limita a poner las celdas en orden. Es
    menos informativo y es lo correcto: inventar un encabezado para un valor
    afirma una relación entre dos textos que en el documento no existe.
    """
    if filas_totales < CABECERA_FILAS_MIN:
        return False
    textos = [c.texto for c in primera_fila.values() if c.texto]
    if len(textos) < 2:
        return False
    largos = sorted(len(x) for x in textos)
    mediana = largos[len(largos) // 2]
    return mediana <= CABECERA_LARGO_MAX


def _render_fila_generica(
    fila: dict[int, Celda], cabeceras: dict[int, str], umbral: float | None
) -> str:
    """Vuelca una fila sin interpretarla, con cada valor junto a su encabezado.

    Es el modo seguro por defecto: no afirma nada sobre el significado de la
    rejilla. Cada fila lleva sus encabezados dentro a propósito — así un corte
    de troceado no puede dejar cifras huérfanas de la columna a la que
    pertenecen, que es la forma silenciosa de que una tabla mienta.
    """
    piezas: list[str] = []
    for columna, celda in sorted(fila.items()):
        encabezado = cabeceras.get(columna)
        if celda.texto:
            piezas.append(f"{encabezado}: {celda.texto}" if encabezado else celda.texto)
        elif celda.marcada(umbral):
            piezas.append(f"{encabezado}: [marcado]" if encabezado else "[marcado]")
    return " | ".join(piezas)


def _render_fila(
    fila: dict[int, Celda],
    cabeceras: dict[int, str],
    centros: dict[int, float],
    columnas: str = "columnas",
    umbral: float | None = None,
) -> str:
    etiqueta = fila.get(1).texto if 1 in fila else ""
    if not etiqueta:
        return ""

    valores = [c for col, c in sorted(fila.items()) if col > 1]
    grupos = _agrupar(c for c in valores if c.texto)
    marcadas = [c for c in valores if c.marcada(umbral)]

    if grupos:
        return f"{etiqueta}: " + "; ".join(
            _frase_grupo(g, cabeceras, centros) for g in grupos
        )
    if marcadas:
        nombres = [cabeceras.get(c.columna, f"columna {c.columna}") for c in marcadas]
        if len(marcadas) == len(valores) and len(valores) > 1:
            return f"{etiqueta}: en todas las {columnas} ({', '.join(nombres)})"
        return f"{etiqueta}: solo en {', '.join(nombres)}"
    # Ni valor ni marca: es un encabezado de sección, o una fila que no se pudo
    # leer. Se emite como encabezado justamente porque un encabezado no afirma
    # nada — si fuera una fila ilegible, emitir la etiqueta suelta invitaría a
    # dar por hecho que la característica está presente.
    return f"\n## {etiqueta}"


def _agrupar(celdas) -> list[list[Celda]]:
    """Reúne palabras contiguas: un valor centrado vuelve partido en celdas."""
    ordenadas = sorted(celdas, key=lambda c: c.texto_izq)
    grupos: list[list[Celda]] = []
    for celda in ordenadas:
        if grupos and celda.texto_izq - grupos[-1][-1].texto_der <= HUECO_MISMO_VALOR:
            grupos[-1].append(celda)
        else:
            grupos.append([celda])
    return grupos


def _frase_grupo(grupo: list[Celda], cabeceras: dict[int, str], centros: dict[int, float]) -> str:
    """A qué columnas pertenece un valor.

    La pregunta se decide comparando dos distancias: del valor al centro del
    área de columnas, y del valor al centro de la columna más cercana. Un valor
    escrito una sola vez en mitad de la tabla —«4,113»— está más cerca del
    centro del área que de ninguna columna, y significa «igual en todas». Uno
    escrito bajo su columna está más cerca de ella. Sobre una ficha real las dos
    distancias salen 0.014 frente a 0.049: no es un empate reñido.
    """
    texto = " ".join(c.texto for c in grupo)
    if not centros:
        return texto

    centro = (grupo[0].texto_izq + grupo[-1].texto_der) / 2
    centro_area = (min(centros.values()) + max(centros.values())) / 2
    if abs(centro - centro_area) < min(abs(centro - x) for x in centros.values()):
        return texto  # vale para todas las columnas

    nombres = [cabeceras.get(c.columna) for c in grupo]
    presentes = list(dict.fromkeys(n for n in nombres if n))
    return f"{texto} ({', '.join(presentes)})" if presentes else texto
