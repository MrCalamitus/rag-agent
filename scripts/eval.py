#!/usr/bin/env python3
"""Evaluación con preguntas de oro (plan E7).

Mide en vez de afirmar: para cada pregunta reporta si la respuesta está
fundamentada, si cita el documento esperado, si declina cuando debe, el TTFT y
la latencia total. Corre contra el despliegue (`--base-url`) o contra la
aplicación local, y compara alias de modelo entre sí.

    python scripts/eval.py                                   # local, alias por defecto
    python scripts/eval.py --models agente-rag-sonnet,agente-rag-haiku
    python scripts/eval.py --base-url https://api.ejemplo --token "$API_TOKEN"

Sale con código 1 si alguna negativa falla o si aparece una cadena prohibida:
una sola credencial inventada invalida la entrega.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from luis_cv.domain.prompts import is_denial as declina  # noqa: E402


@dataclass
class Resultado:
    id: str
    modelo: str
    tipo: str
    correcto: bool
    motivo: str
    documentos: list[str]
    ttft_s: float
    total_s: float
    respuesta: str


@contextlib.contextmanager
def app_local(corpus: str):
    """Levanta la aplicación con los adaptadores locales, en un hilo."""
    import uvicorn

    os.environ.setdefault("LUISCV_API_TOKEN", "local-dev-token")
    os.environ.setdefault("LUISCV_LOG_LEVEL", "WARNING")
    os.environ["LUISCV_CORPUS_DIR"] = corpus

    from luis_cv.infrastructure.inbound.http.app import create_app

    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        puerto = int(s.getsockname()[1])

    servidor = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=puerto, log_level="error"))
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    while not servidor.started:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{puerto}", os.environ["LUISCV_API_TOKEN"]
    finally:
        servidor.should_exit = True
        hilo.join(timeout=10)


def preguntar(cliente: httpx.Client, base: str, token: str, modelo: str, pregunta: str):
    cuerpo = {"model": modelo, "input": pregunta, "stream": True}
    cabeceras = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    inicio = time.perf_counter()
    ttft = float("nan")
    texto: list[str] = []
    documentos: list[str] = []
    with cliente.stream("POST", f"{base}/v1/responses", json=cuerpo, headers=cabeceras, timeout=120) as r:
        r.raise_for_status()
        for linea in r.iter_lines():
            if not linea.startswith("data:"):
                continue
            crudo = linea[5:].strip()
            if crudo == "[DONE]":
                break
            evento = json.loads(crudo)
            if evento["type"] == "response.output_text.delta":
                if ttft != ttft:  # NaN: primer delta
                    ttft = time.perf_counter() - inicio
                texto.append(evento["delta"])
            elif evento["type"] == "response.output_item.done":
                item = evento["item"]
                if item["type"].endswith("knowledge_search"):
                    documentos = [r_["document_id"] for r_ in item["results"]]
    return "".join(texto), documentos, ttft, time.perf_counter() - inicio


def evaluar(caso: dict, respuesta: str, documentos: list[str]) -> tuple[bool, str]:
    """Tres tipos de caso, porque la respuesta correcta no siempre es la misma.

    - `positiva`: hay evidencia → responde y cita el documento.
    - `negativa`: no hay evidencia → declina, sin inventar.
    - `enmascarada`: hay evidencia pero es un identificador → confirma que
      existe **sin** transcribirlo íntegro (contrato §6.2). Declinar aquí sería
      tan incorrecto como filtrarlo: el agente sí sabe que la credencial
      existe.
    """
    # `prohibido` es para datos literales que jamás deben salir (un CURP, un
    # domicilio, unos dígitos), nunca para palabras del tema: negar una
    # credencial obliga a nombrarla.
    for prohibido in caso.get("prohibido", []):
        if prohibido.lower() in respuesta.lower():
            return False, f"filtró una cadena prohibida: {prohibido!r}"

    negada = declina(respuesta)
    tipo = caso["tipo"]

    if tipo == "negativa":
        return (negada, "niega como debe" if negada else "NO negó: posible invención")

    esperado = caso.get("documento_esperado")
    if esperado and esperado not in documentos:
        return False, f"no recuperó {esperado}"

    if tipo == "enmascarada":
        # Dos formas correctas: dar el identificador enmascarado, o confirmar
        # que existe y negarse a transcribirlo. Ambas cumplen el §6.2. Lo que
        # falla es afirmar que no consta teniendo el documento delante, y eso
        # se detecta por la ausencia de cita, no por el tono de la respuesta.
        if esperado and f"[{esperado}]" not in respuesta:
            return False, f"tiene {esperado} pero no lo citó al responder"
        return True, "confirma la credencial sin transcribir el identificador"

    if negada and esperado and f"[{esperado}]" not in respuesta:
        return False, "declinó teniendo la evidencia recuperada"
    if f"[{esperado}]" not in respuesta:
        return False, f"recuperó {esperado} pero no lo citó"
    return True, "fundamentada y citada"


def reporte(resultados: list[Resultado]) -> str:
    lineas = [
        f"# Evaluación — {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "| Caso | Modelo | Tipo | Veredicto | Motivo | TTFT | Total |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in resultados:
        lineas.append(
            f"| {r.id} | {r.modelo} | {r.tipo} | {'✅' if r.correcto else '❌'} | {r.motivo} "
            f"| {r.ttft_s:.2f}s | {r.total_s:.2f}s |"
        )
    lineas += ["", "## Resumen por modelo", "", "| Modelo | Aciertos | Negativas OK | TTFT p50 |", "|---|---|---|---|"]
    for modelo in dict.fromkeys(r.modelo for r in resultados):
        del_modelo = [r for r in resultados if r.modelo == modelo]
        negativas = [r for r in del_modelo if r.tipo == "negativa"]
        ttfts = sorted(r.ttft_s for r in del_modelo if r.ttft_s == r.ttft_s)
        p50 = ttfts[len(ttfts) // 2] if ttfts else float("nan")
        lineas.append(
            f"| {modelo} | {sum(r.correcto for r in del_modelo)}/{len(del_modelo)} "
            f"| {sum(r.correcto for r in negativas)}/{len(negativas)} | {p50:.2f}s |"
        )
    return "\n".join(lineas) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Preguntas de oro contra el agente")
    parser.add_argument("--base-url", default=os.getenv("BASE_URL"))
    parser.add_argument("--token", default=os.getenv("API_TOKEN", "local-dev-token"))
    parser.add_argument("--golden", default=str(RAIZ / "tests" / "golden.yaml"))
    parser.add_argument("--models", default="agente-rag-sonnet")
    parser.add_argument("--out", default=str(RAIZ / "reports"))
    args = parser.parse_args()

    golden = yaml.safe_load(Path(args.golden).read_text(encoding="utf-8"))
    modelos = [m.strip() for m in args.models.split(",") if m.strip()]

    contexto = (
        contextlib.nullcontext((args.base_url, args.token))
        if args.base_url
        else app_local(golden.get("corpus", "corpus"))
    )
    resultados: list[Resultado] = []
    with contexto as (base, token), httpx.Client() as cliente:
        print(f"Evaluando contra {base} — {len(golden['preguntas'])} preguntas × {len(modelos)} modelo(s)\n")
        for modelo in modelos:
            for caso in golden["preguntas"]:
                respuesta, documentos, ttft, total = preguntar(
                    cliente, base, token, modelo, caso["pregunta"]
                )
                correcto, motivo = evaluar(caso, respuesta, documentos)
                resultados.append(
                    Resultado(
                        caso["id"], modelo, caso["tipo"], correcto, motivo, documentos, ttft, total, respuesta
                    )
                )
                print(f"  {'✅' if correcto else '❌'} {caso['id']} [{modelo}] {motivo}")

    salida = Path(args.out)
    salida.mkdir(parents=True, exist_ok=True)
    archivo = salida / f"eval-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.md"
    archivo.write_text(reporte(resultados), encoding="utf-8")
    print(f"\nReporte: {archivo}")

    negativas_fallidas = [r for r in resultados if r.tipo == "negativa" and not r.correcto]
    if negativas_fallidas:
        print(f"\n❌ {len(negativas_fallidas)} negativa(s) fallaron: hay invención. Entrega bloqueada.")
        return 1
    fallos = [r for r in resultados if not r.correcto]
    print(f"\n{'✅' if not fallos else '⚠️'} {len(resultados) - len(fallos)}/{len(resultados)} casos correctos.")
    return 0 if not fallos else 2


if __name__ == "__main__":
    raise SystemExit(main())
