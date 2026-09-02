"""OCR lineal con tesseract: el motor gratuito y sin red.

Se invoca el binario por subproceso en vez de usar `pytesseract`: esa biblioteca
es un envoltorio de subproceso, y evitarla ahorra una dependencia sin perder
nada.

**Qué NO hace, y por qué importa.** Tesseract lee en orden de lectura y aplana
la estructura. Sobre prosa —un informe, un contrato, un manual— es perfectamente
válido. Sobre una ficha comparativa devuelve las etiquetas y pierde a qué
columna pertenecía cada valor, con lo que una fila que solo aplica a una versión
queda escrita como si aplicara a todas. Por eso el perfil elige el motor de
forma explícita y esta limitación se avisa por escrito al usarlo.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import PaginaExtraida, ResultadoOcr


class TesseractOcr:
    nombre = "texto"

    def __init__(self, binario: str = "tesseract") -> None:
        self._binario = binario

    # -- protocolo ------------------------------------------------------
    def disponible(self) -> tuple[bool, str]:
        if shutil.which(self._binario) is None:
            return False, "tesseract no está instalado (macOS: brew install tesseract)"
        return True, ""

    def paginas_por_documento(self, total: int) -> int:
        return total

    def idiomas(self) -> tuple[str, ...]:
        try:
            salida = subprocess.run(
                [self._binario, "--list-langs"], capture_output=True, text=True, timeout=20
            )
        except (subprocess.SubprocessError, OSError):  # pragma: no cover
            return ()
        lineas = (salida.stdout or salida.stderr).splitlines()
        return tuple(l.strip() for l in lineas[1:] if l.strip())

    def extraer(self, imagenes: list[bytes], *, idioma: str = "spa") -> ResultadoOcr:
        resultado = ResultadoOcr(motor=self.nombre)
        disponibles = self.idiomas()
        if disponibles and idioma not in disponibles:
            # Leer español con el modelo inglés destroza los acentos y confunde
            # cifras. Vale más decirlo que entregar una transcripción sucia en
            # silencio y que alguien la cite como si fuera el original.
            resultado.avisos.append(
                f"el paquete de idioma '{idioma}' no está instalado "
                f"(hay: {', '.join(disponibles)}). macOS: brew install tesseract-lang. "
                f"Se transcribe con 'eng' y la calidad sobre texto en español baja."
            )
            idioma = "eng" if "eng" in disponibles else disponibles[0]

        resultado.avisos.append(
            "OCR lineal: se pierde la estructura de tabla. Si el documento es un "
            "comparativo por columnas, usa el motor 'tablas'."
        )
        for numero, imagen in enumerate(imagenes, start=1):
            try:
                resultado.paginas.append(
                    PaginaExtraida(numero=numero, texto=self._una(imagen, idioma))
                )
            except Exception as exc:  # noqa: BLE001 - una página mala no tumba el documento
                resultado.avisos.append(f"página {numero}: {type(exc).__name__}: {exc}")
        return resultado

    # -- interno --------------------------------------------------------
    def _una(self, imagen: bytes, idioma: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            entrada = Path(tmp) / "pagina.png"
            entrada.write_bytes(imagen)
            proceso = subprocess.run(
                [self._binario, str(entrada), "stdout", "-l", idioma, "--psm", "3"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proceso.returncode != 0:
                raise RuntimeError((proceso.stderr or "tesseract falló").strip()[:200])
            return proceso.stdout.strip()
