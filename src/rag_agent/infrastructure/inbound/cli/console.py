"""Utilidades de terminal, sin dependencias.

Deliberadamente pobre: `input()`, ANSI y nada más. Una biblioteca de TUI daría
flechas y colores más finos, pero este menú es lo primero que ejecuta alguien
que acaba de clonar el repositorio —posiblemente antes de tener el entorno
creado— y fallar con un `ModuleNotFoundError` en el paso 1 sería el peor
arranque posible.

Todo lo que pide datos acepta Enter como «lo que ya había»: reconfigurar un
proyecto no debería obligar a reescribir seis valores para cambiar uno.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(codigo: str, texto: str) -> str:
    return f"\033[{codigo}m{texto}\033[0m" if _COLOR else texto


def negrita(t: str) -> str:
    return _c("1", t)


def apagado(t: str) -> str:
    return _c("2", t)


def verde(t: str) -> str:
    return _c("32", t)


def amarillo(t: str) -> str:
    return _c("33", t)


def rojo(t: str) -> str:
    return _c("31", t)


def cian(t: str) -> str:
    return _c("36", t)


class Cancelado(Exception):
    """El usuario cortó con Ctrl-C o Ctrl-D."""


def titulo(texto: str) -> None:
    print(f"\n{negrita(texto)}\n{apagado('─' * max(len(texto), 40))}")


def aviso(texto: str) -> None:
    print(f"{amarillo('⚠')} {texto}")


def error(texto: str) -> None:
    print(f"{rojo('✗')} {texto}")


def exito(texto: str) -> None:
    print(f"{verde('✓')} {texto}")


def paso(texto: str) -> None:
    print(f"{cian('▸')} {texto}")


def _leer(prompt: str) -> str:
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelado from None


def preguntar(etiqueta: str, *, defecto: str | None = None, obligatorio: bool = True) -> str:
    """Pide un valor. Enter deja el que ya había."""
    sufijo = f" {apagado(f'[{defecto}]')}" if defecto else ""
    while True:
        valor = _leer(f"  {etiqueta}{sufijo}: ").strip()
        if not valor and defecto is not None:
            return defecto
        if valor or not obligatorio:
            return valor
        error("Ese valor es obligatorio.")


def confirmar(etiqueta: str, *, defecto: bool = True) -> bool:
    opciones = "S/n" if defecto else "s/N"
    respuesta = _leer(f"  {etiqueta} {apagado(f'[{opciones}]')}: ").strip().lower()
    if not respuesta:
        return defecto
    return respuesta in ("s", "si", "sí", "y", "yes")


@dataclass(frozen=True)
class Opcion:
    clave: str
    etiqueta: str
    detalle: str = ""


def elegir(opciones: list[Opcion], *, prompt: str = "Elige una opción") -> str:
    """Menú numerado. Devuelve la clave elegida.

    Se aceptan tanto el número como la clave: quien ya se sabe el menú escribe
    `deploy` y no cuenta líneas.
    """
    ancho = max(len(o.etiqueta) for o in opciones)
    for indice, opcion in enumerate(opciones, start=1):
        numero = "0" if opcion.clave == "salir" else str(indice)
        relleno = "." * (ancho + 3 - len(opcion.etiqueta))
        detalle = f" {apagado(relleno)} {apagado(opcion.detalle)}" if opcion.detalle else ""
        print(f"  {negrita(numero)}) {opcion.etiqueta}{detalle}")
    print()
    while True:
        elegida = _leer(f"  {prompt}: ").strip().lower()
        if not elegida:
            continue
        if elegida in ("0", "q", "salir", "exit"):
            return "salir"
        if elegida.isdigit() and 1 <= int(elegida) <= len(opciones):
            return opciones[int(elegida) - 1].clave
        for opcion in opciones:
            if opcion.clave == elegida:
                return opcion.clave
        error("Opción no reconocida.")


def tabla(filas: list[tuple[str, str]], *, sangria: str = "  ") -> None:
    if not filas:
        return
    ancho = max(len(clave) for clave, _ in filas)
    for clave, valor in filas:
        print(f"{sangria}{apagado(clave.ljust(ancho))}  {valor}")


def slugificar(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", texto.lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", plano)).strip("-")


def interactivo() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
