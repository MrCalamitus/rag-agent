"""Troceado de documentos largos en fragmentos citables.

Vive en el dominio porque decide qué es un «fragmento», y un fragmento es la
unidad de evidencia que el agente cita. El corpus de credenciales podía tratar
cada documento como un fragmento único —un título cabe en media página— pero un
folleto de coche de 40 páginas como fragmento único produce dos fallos a la vez:
el agente cita «el folleto» para cualquier afirmación, sin poder señalar dónde
lo dice, y cada turno paga decenas de miles de tokens de prompt.

Tres propiedades que el troceo debe cumplir:

1. **Cortar por costuras del texto, no por posición.** Se prefiere el límite de
   sección (encabezado Markdown), luego el de párrafo, luego el de frase. Un
   corte a mitad de tabla de especificaciones separa la cifra de su etiqueta y
   produce un fragmento que afirma «187» sin decir de qué.
2. **Solapar.** El dato que cae justo en la costura tiene que aparecer entero
   en uno de los dos lados. El solape se toma del final del fragmento anterior,
   respetando también el límite de párrafo o frase.
3. **Repetir la cabecera de una tabla partida.** Una fila de ficha técnica sin
   su cabecera es un renglón de viñetas del que no se sabe a qué versión
   pertenece. Partir la tabla sin repetirla deshace en el troceo justo lo que
   costó recuperar al extraer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .profile import ChunkPolicy

# Costuras, de más fuerte a más débil. Un encabezado Markdown abre sección; una
# línea en blanco separa párrafos; un punto seguido separa frases.
_ENCABEZADO = re.compile(r"^#{1,6} .+$", re.MULTILINE)
_PARRAFO = re.compile(r"\n\s*\n")
_FRASE = re.compile(r"(?<=[.!?:;])\s+")
# Línea de guiones de una tabla Markdown: `|---|---:|`, con o sin barras
# en los extremos. Tiene que haber al menos un guion.
_SEPARADOR_TABLA = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


@dataclass(frozen=True)
class TextChunk:
    """Un fragmento con su posición en el documento de origen."""

    index: int
    total: int
    text: str

    @property
    def is_whole_document(self) -> bool:
        return self.total == 1


def _bloques(texto: str) -> list[str]:
    """Parte el texto en las unidades más pequeñas que no conviene romper."""
    secciones: list[str] = []
    cortes = [m.start() for m in _ENCABEZADO.finditer(texto)]
    if cortes:
        # El preámbulo anterior al primer encabezado es una sección más.
        limites = ([0] if cortes[0] > 0 else []) + cortes + [len(texto)]
        secciones = [texto[a:b] for a, b in zip(limites, limites[1:]) if texto[a:b].strip()]
    else:
        secciones = [texto]

    bloques: list[str] = []
    for seccion in secciones:
        for parrafo in _PARRAFO.split(seccion):
            if parrafo.strip():
                bloques.append(parrafo.strip())
    return bloques


def _cabecera_de_tabla(bloque: str) -> tuple[str, list[str]] | None:
    """Cabecera y filas de una tabla Markdown, o `None` si el bloque no lo es.

    La segunda línea —la de guiones— es la firma inequívoca del formato, y
    basta con ella: cualquier otra heurística confundiría una tabla con un
    listado que empieza por barra.
    """
    lineas = bloque.splitlines()
    if len(lineas) < 3 or not lineas[0].lstrip().startswith("|"):
        return None
    if not _SEPARADOR_TABLA.match(lineas[1]):
        return None
    corte = 2
    # Un rótulo que ocupa el ancho de la tabla —«MAZDA3 SEDÁN 2026» repetido en
    # todas las celdas— es la primera fila, pero los nombres de las columnas
    # están en la siguiente. Repetir solo el rótulo daría un fragmento que dice
    # de qué modelo habla y no de qué versión, que es la pregunta que se hace.
    if len(lineas) > 3 and _es_rotulo(lineas[0]):
        corte = 3
    return "\n".join(lineas[:corte]), lineas[corte:]


def _es_rotulo(fila: str) -> bool:
    """Fila cuyas celdas dicen todas lo mismo: un título que abarca la tabla."""
    celdas = [c.strip() for c in fila.strip().strip("|").split("|")]
    llenas = {c for c in celdas if c}
    return len(celdas) > 1 and len(llenas) == 1


def _unir(unidades: list[str], max_chars: int, junta: str = "\n") -> list[str]:
    """Agrupa unidades en piezas que no pasen del máximo, sin partir ninguna."""
    piezas: list[str] = []
    actual = ""
    for unidad in unidades:
        unidad = unidad.strip()
        if not unidad:
            continue
        candidato = f"{actual}{junta}{unidad}" if actual else unidad
        if actual and len(candidato) > max_chars:
            piezas.append(actual)
            actual = unidad
        else:
            actual = candidato
    if actual:
        piezas.append(actual)
    return piezas


def _partir_bloque(bloque: str, max_chars: int) -> list[str]:
    """Un bloque que ya excede el máximo se parte por frases, y si no hay
    frases —una tabla larga, un listado— por líneas. Nunca a mitad de línea:
    una fila de especificaciones cortada por la mitad no es evidencia.

    Una tabla se parte además **repitiendo su cabecera en cada pieza**. Sin eso
    el troceo deshace justo lo que hace valiosa a una tabla: el fragmento que
    dice `| Espejo electrocrómico | - | • | • |` llega a la recuperación sin
    saber que esas columnas son las versiones, y el agente no puede responder
    de cuál habla. La cabecera cuesta unos cientos de caracteres por fragmento
    y es la diferencia entre un dato y un renglón de viñetas sueltas.
    """
    if len(bloque) <= max_chars:
        return [bloque]

    if (tabla := _cabecera_de_tabla(bloque)) is not None:
        cabecera, filas = tabla
        # Se descuenta la cabecera del presupuesto: se repite en cada pieza, así
        # que tiene que caber *dentro* del máximo y no por encima de él.
        cuerpo = _unir(filas, max(1, max_chars - len(cabecera) - 1))
        return [f"{cabecera}\n{pieza}" for pieza in cuerpo]

    unidades = _FRASE.split(bloque)
    if len(unidades) > 1:
        # Frases: se reconstruyen con espacio, que es como estaban escritas.
        return _unir(unidades, max_chars, junta=" ")
    # Líneas: se reconstruyen con salto. Unirlas con espacio aplastaba en un
    # solo renglón las filas de cualquier tabla que hubiera que partir.
    return _unir(bloque.splitlines(), max_chars)


def _cola(texto: str, overlap: int) -> str:
    """Últimos `overlap` caracteres, extendidos hasta una costura hacia atrás.

    De ahí se descartan las filas de tabla: una fila sin su cabecera es el
    renglón de viñetas anónimo que el troceo se ocupa de no producir, y la pieza
    que abre el fragmento siguiente ya viene con la suya. Solapar aquí solo
    añadiría ruido delante del dato bueno.
    """
    if overlap <= 0:
        return ""
    recorte = texto if len(texto) <= overlap else texto[-overlap:]
    if len(texto) > overlap:
        for separador in ("\n\n", ". ", "\n", " "):
            posicion = recorte.find(separador)
            if 0 <= posicion < len(recorte) - 1:
                recorte = recorte[posicion + len(separador) :]
                break
    sin_filas = [l for l in recorte.splitlines() if not l.lstrip().startswith("|")]
    return "\n".join(sin_filas).strip()


def split(texto: str, policy: ChunkPolicy) -> tuple[TextChunk, ...]:
    """Trocea un documento según la política del perfil.

    Un documento por debajo de `min_chars_to_split` sale entero: es la
    estrategia del corpus de credenciales, conservada como caso particular en
    lugar de como excepción en el código de llamada.
    """
    limpio = texto.strip()
    if not limpio:
        return ()
    if len(limpio) < policy.min_chars_to_split:
        return (TextChunk(index=1, total=1, text=limpio),)

    piezas: list[str] = []
    actual = ""
    # Las piezas se acotan a `max_chars - overlap`: al reabrir un fragmento se le
    # antepone la cola del anterior, y sin este margen el resultado excedería el
    # máximo justo en los fragmentos que siguen a un corte.
    tope_pieza = max(1, policy.max_chars - policy.overlap_chars)
    for bloque in _bloques(limpio):
        for parte in _partir_bloque(bloque, tope_pieza):
            candidato = f"{actual}\n\n{parte}" if actual else parte
            if actual and len(candidato) > policy.max_chars:
                piezas.append(actual)
                cola = _cola(actual, policy.overlap_chars)
                actual = f"{cola}\n\n{parte}" if cola else parte
            else:
                actual = candidato
    if actual.strip():
        piezas.append(actual.strip())

    total = len(piezas)
    return tuple(
        TextChunk(index=i, total=total, text=pieza.strip())
        for i, pieza in enumerate(piezas, start=1)
    )


def chunk_document_id(stem: str, chunk: TextChunk) -> str:
    """`document_id` legible en la cita: `hilux-2024` o `hilux-2024--003`.

    El sufijo solo aparece cuando hay más de un fragmento, para que el corpus
    de documentos cortos siga citando exactamente como antes.
    """
    if chunk.is_whole_document:
        return stem
    return f"{stem}--{chunk.index:03d}"
