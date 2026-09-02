"""Troceado de documentos largos en fragmentos citables.

Vive en el dominio porque decide qué es un «fragmento», y un fragmento es la
unidad de evidencia que el agente cita. El corpus de credenciales podía tratar
cada documento como un fragmento único —un título cabe en media página— pero un
folleto de coche de 40 páginas como fragmento único produce dos fallos a la vez:
el agente cita «el folleto» para cualquier afirmación, sin poder señalar dónde
lo dice, y cada turno paga decenas de miles de tokens de prompt.

Dos propiedades que el troceo debe cumplir:

1. **Cortar por costuras del texto, no por posición.** Se prefiere el límite de
   sección (encabezado Markdown), luego el de párrafo, luego el de frase. Un
   corte a mitad de tabla de especificaciones separa la cifra de su etiqueta y
   produce un fragmento que afirma «187» sin decir de qué.
2. **Solapar.** El dato que cae justo en la costura tiene que aparecer entero
   en uno de los dos lados. El solape se toma del final del fragmento anterior,
   respetando también el límite de párrafo o frase.
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


def _partir_bloque(bloque: str, max_chars: int) -> list[str]:
    """Un bloque que ya excede el máximo se parte por frases, y si no hay
    frases —una tabla larga, un listado— por líneas. Nunca a mitad de línea:
    una fila de especificaciones cortada por la mitad no es evidencia."""
    if len(bloque) <= max_chars:
        return [bloque]
    piezas: list[str] = []
    actual = ""
    unidades = _FRASE.split(bloque)
    if len(unidades) == 1:
        unidades = bloque.splitlines()
    for unidad in unidades:
        unidad = unidad.strip()
        if not unidad:
            continue
        candidato = f"{actual} {unidad}".strip() if actual else unidad
        if actual and len(candidato) > max_chars:
            piezas.append(actual)
            actual = unidad
        else:
            actual = candidato
    if actual:
        piezas.append(actual)
    return piezas


def _cola(texto: str, overlap: int) -> str:
    """Últimos `overlap` caracteres, extendidos hasta una costura hacia atrás."""
    if overlap <= 0 or len(texto) <= overlap:
        return texto if overlap > 0 else ""
    recorte = texto[-overlap:]
    for separador in ("\n\n", ". ", "\n", " "):
        posicion = recorte.find(separador)
        if 0 <= posicion < len(recorte) - 1:
            return recorte[posicion + len(separador) :].strip()
    return recorte.strip()


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
