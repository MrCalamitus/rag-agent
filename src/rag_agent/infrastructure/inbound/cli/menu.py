"""Menú interactivo: el punto de entrada humano del proyecto.

Es un adaptador de entrada como el HTTP, solo que el actor es una persona en
una terminal en vez de un cliente de API. No decide nada de negocio: resuelve
la configuración, llama a los mismos casos de uso y a los mismos scripts que se
usarían a mano, y enseña el estado.

Su razón de ser es que reutilizar este agente en un tema nuevo son cinco pasos
—configurar, preparar el corpus, probar, desplegar, ingerir— y cada uno tiene
su comando con sus parámetros. El menú los pone en orden y, sobre todo, dice en
cuál estás: la mitad de los errores de un despliegue son creer que ya se hizo
el paso anterior.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import console as c
from .init import ejecutar as ejecutar_init
from .init import leer_pares, _ENV, _TFVAR, preguntar_perfil

RAIZ = Path(__file__).resolve().parents[5]


@dataclass
class Estado:
    """Lo que está configurado y lo que falta, en un vistazo."""

    proyecto: str | None
    region: str | None
    cuenta: str | None
    perfil_activo: str | None
    perfiles: dict
    backends: tuple[str, str]

    @property
    def inicializado(self) -> bool:
        return bool(self.proyecto and self.perfiles)


def cargar_estado() -> Estado:
    env = leer_pares(RAIZ / ".env", _ENV)
    tf = leer_pares(RAIZ / "infra" / "terraform.tfvars", _TFVAR)
    try:
        from ...profiles import load_profiles

        perfiles = load_profiles(RAIZ / (env.get("RAG_PROFILES_DIR") or "profiles"))
    except Exception as exc:  # noqa: BLE001 - un YAML roto no debe tumbar el menú
        c.error(f"No se pudieron leer los perfiles: {exc}")
        perfiles = {}
    return Estado(
        proyecto=tf.get("project"),
        region=tf.get("aws_region"),
        cuenta=tf.get("aws_account_id"),
        perfil_activo=env.get("RAG_DEFAULT_PROFILE") or (next(iter(perfiles), None)),
        perfiles=perfiles,
        backends=(
            env.get("RAG_RETRIEVAL_BACKEND", "local"),
            env.get("RAG_INFERENCE_BACKEND", "stub"),
        ),
    )


def _carpeta(valor: str | None) -> Path | None:
    if not valor:
        return None
    ruta = Path(valor).expanduser()
    return ruta if ruta.is_absolute() else RAIZ / ruta


def _cuenta_archivos(carpeta: Path | None, patron: str) -> int:
    return len(list(carpeta.rglob(patron))) if carpeta and carpeta.is_dir() else 0


# --- acciones -----------------------------------------------------------------


def accion_init(estado: Estado) -> None:
    slug = ejecutar_init(RAIZ, existentes=tuple(estado.perfiles))
    c.exito(f"Proyecto configurado. Tema activo: {slug}")
    print(f"\n  Siguiente: {c.negrita('3) Preparar corpus')} para indexar los documentos.")


def accion_perfiles(estado: Estado) -> None:
    while True:
        c.titulo("Temas")
        if not estado.perfiles:
            c.aviso("No hay ningún tema definido todavía.")
        else:
            filas = []
            for slug, binding in estado.perfiles.items():
                marca = c.verde("●") if slug == estado.perfil_activo else c.apagado("○")
                origen = _carpeta(binding.source_dir)
                preparado = _carpeta(binding.prepared_dir)
                filas.append(
                    (
                        f"{marca} {slug}",
                        f"{binding.profile.name} · {_cuenta_archivos(origen, '*.pdf')} PDF"
                        f" · {_cuenta_archivos(preparado, '*.md')} fragmentos preparados",
                    )
                )
            c.tabla(filas)
        print()
        opcion = c.elegir(
            [
                c.Opcion("nuevo", "Crear un tema", "asistente de perfil"),
                c.Opcion("activar", "Activar un tema", "cambia RAG_DEFAULT_PROFILE"),
                c.Opcion("ver", "Ver las reglas de un tema", "abre el YAML"),
                c.Opcion("salir", "Volver"),
            ]
        )
        if opcion == "salir":
            return
        if opcion == "nuevo":
            ruta = preguntar_perfil(RAIZ, existentes=tuple(estado.perfiles))
            c.exito(f"Creado {ruta.relative_to(RAIZ)}")
            estado = cargar_estado()
        elif opcion == "activar":
            slug = _elegir_perfil(estado, "Tema a activar")
            if slug:
                _fijar_perfil_activo(slug)
                c.exito(f"Tema activo: {slug}")
                estado = cargar_estado()
        elif opcion == "ver":
            slug = _elegir_perfil(estado, "Tema")
            if slug:
                ruta = RAIZ / "profiles" / f"{slug}.yaml"
                print()
                print(c.apagado(ruta.read_text(encoding="utf-8")))


def _elegir_perfil(estado: Estado, prompt: str) -> str | None:
    if not estado.perfiles:
        c.aviso("No hay temas definidos.")
        return None
    if len(estado.perfiles) == 1:
        return next(iter(estado.perfiles))
    opciones = [
        c.Opcion(slug, slug, binding.profile.name) for slug, binding in estado.perfiles.items()
    ] + [c.Opcion("salir", "Cancelar")]
    elegido = c.elegir(opciones, prompt=prompt)
    return None if elegido == "salir" else elegido


def _fijar_perfil_activo(slug: str) -> None:
    ruta = RAIZ / ".env"
    lineas = ruta.read_text(encoding="utf-8").splitlines() if ruta.is_file() else []
    salida, encontrada = [], False
    for linea in lineas:
        if linea.startswith("RAG_DEFAULT_PROFILE="):
            salida.append(f"RAG_DEFAULT_PROFILE={slug}")
            encontrada = True
        else:
            salida.append(linea)
    if not encontrada:
        salida.append(f"RAG_DEFAULT_PROFILE={slug}")
    ruta.write_text("\n".join(salida) + "\n", encoding="utf-8")


def accion_corpus(estado: Estado) -> None:
    slug = _elegir_perfil(estado, "Tema a preparar")
    if not slug:
        return
    binding = estado.perfiles[slug]
    origen = _carpeta(binding.source_dir)
    if origen is None or not origen.is_dir():
        c.error(f"El perfil '{slug}' no tiene carpeta de origen legible: {binding.source_dir}")
        return

    c.titulo(f"Preparando el corpus de {slug}")
    c.tabla(
        [
            ("origen", str(origen)),
            ("destino", str(_carpeta(binding.prepared_dir))),
            ("documentos", str(_cuenta_archivos(origen, "*.*"))),
            ("troceado", f"{binding.profile.chunking.max_chars} car. con {binding.profile.chunking.overlap_chars} de solape"),
        ]
    )
    print()
    if not c.confirmar("¿Continuar?"):
        return
    _ejecutar([sys.executable, str(RAIZ / "scripts" / "prep_corpus.py"), "--profile", slug])


def accion_chat(estado: Estado) -> None:
    """Conversación local contra el agente, con el tema activo."""
    slug = _elegir_perfil(estado, "Tema") or estado.perfil_activo
    if not slug:
        return
    asyncio.run(_chat(slug, estado))


async def _chat(slug: str, estado: Estado) -> None:
    from ....infrastructure.config import Settings
    from ....infrastructure.container import build_container
    from ....application.commands import CreateResponseCommand
    from ....domain.conversation import Conversation, Role, Turn

    ajustes = Settings(default_profile=slug, profiles_dir=str(RAIZ / "profiles"))
    try:
        contenedor = build_container(ajustes)
    except Exception as exc:  # noqa: BLE001 - configuración incompleta es lo normal aquí
        c.error(f"No se pudo levantar el agente: {exc}")
        return

    binding = estado.perfiles.get(slug)
    preparado = _carpeta(binding.prepared_dir) if binding else None
    fragmentos = _cuenta_archivos(preparado, "*.md")
    c.titulo(f"Chat — {slug}")
    c.tabla(
        [
            ("recuperación", f"{ajustes.retrieval_backend} ({fragmentos} fragmentos)"),
            ("inferencia", f"{ajustes.inference_backend} · {ajustes.default_model}"),
        ]
    )
    if ajustes.retrieval_backend == "local" and fragmentos == 0:
        c.aviso("El corpus preparado está vacío: el agente declinará todo. Prepara el corpus antes.")
    if ajustes.inference_backend == "stub":
        c.aviso("Inferencia en modo stub: responde con plantillas, no con un modelo real.")
        print(c.apagado("    Para usar Bedrock: RAG_INFERENCE_BACKEND=bedrock en .env"))
    print(c.apagado("\n  Escribe tu pregunta. Enter en blanco para volver al menú.\n"))

    turnos: list[Turn] = []
    while True:
        try:
            pregunta = input(f"  {c.cian('tú')} › ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not pregunta:
            return
        turnos.append(Turn(Role.USER, pregunta))
        comando = CreateResponseCommand(
            model_alias=ajustes.default_model,
            conversation=Conversation(tuple(turnos)),
            profile_slug=slug,
            request_id="menu",
        )
        try:
            respuesta = await contenedor.create_response.execute(comando)
        except Exception as exc:  # noqa: BLE001 - un fallo no debe cerrar el chat
            c.error(str(exc))
            turnos.pop()
            continue
        texto = "".join(
            item.text for item in respuesta.output if getattr(item, "text", None)
        )
        documentos = [
            doc
            for item in respuesta.output
            for doc in getattr(getattr(item, "outcome", None), "documents", lambda: ())()
        ]
        print(f"\n  {c.verde('agente')} › {texto}\n")
        if documentos:
            print(c.apagado(f"    evidencia: {', '.join(documentos)}\n"))
        turnos.append(Turn(Role.ASSISTANT, texto))


def accion_desplegar(estado: Estado) -> None:
    if not estado.cuenta:
        c.error("Falta la cuenta de AWS. Ejecuta primero la inicialización.")
        return
    c.titulo("Despliegue")
    c.tabla(
        [
            ("proyecto", f"{estado.proyecto} en {estado.region}"),
            ("cuenta", estado.cuenta),
            ("temas", ", ".join(estado.perfiles) or "(ninguno)"),
        ]
    )
    print()
    opcion = c.elegir(
        [
            c.Opcion("plan", "Ver el plan", "no cambia nada"),
            c.Opcion("apply", "Desplegar", "build + push + apply + espera"),
            c.Opcion("salir", "Volver"),
        ]
    )
    if opcion == "salir":
        return
    _ejecutar(["./scripts/deploy.sh"] + ([] if opcion == "apply" else ["plan"]))


def accion_sync(estado: Estado) -> None:
    slug = _elegir_perfil(estado, "Tema a sincronizar")
    if not slug:
        return
    binding = estado.perfiles[slug]
    preparado = _carpeta(binding.prepared_dir)
    if not preparado or not preparado.is_dir():
        c.error(f"No hay corpus preparado para '{slug}'. Prepáralo antes de sincronizar.")
        return
    c.titulo(f"Ingesta de {slug}")
    print(f"  {_cuenta_archivos(preparado, '*.md')} fragmentos desde {preparado}\n")
    if c.confirmar("¿Subir a S3 y lanzar la ingesta?"):
        _ejecutar(["./scripts/sync-kb.sh", str(preparado)], entorno={"RAG_PROFILE": slug})


def accion_evaluar(estado: Estado) -> None:
    slug = _elegir_perfil(estado, "Tema a evaluar")
    if not slug:
        return
    dorado = RAIZ / "tests" / f"golden-{slug}.yaml"
    if not dorado.is_file():
        dorado = RAIZ / "tests" / "golden.yaml"
        c.aviso(f"No hay preguntas de oro para '{slug}'; se usa {dorado.name}.")
        print(c.apagado(f"    Crea tests/golden-{slug}.yaml para medir este tema de verdad."))
    _ejecutar([sys.executable, str(RAIZ / "scripts" / "eval.py"), "--golden", str(dorado)])


def accion_estado(estado: Estado) -> None:
    c.titulo("Estado")
    pendientes: list[str] = []

    c.tabla(
        [
            ("proyecto", estado.proyecto or c.rojo("sin configurar")),
            ("cuenta / región", f"{estado.cuenta or '—'} / {estado.region or '—'}"),
            ("backends", f"recuperación={estado.backends[0]}  inferencia={estado.backends[1]}"),
            ("tema activo", estado.perfil_activo or c.rojo("ninguno")),
        ]
    )
    if not estado.proyecto:
        pendientes.append("Ejecutar la inicialización (opción 1)")

    print()
    for slug, binding in estado.perfiles.items():
        origen = _carpeta(binding.source_dir)
        preparado = _carpeta(binding.prepared_dir)
        originales = _cuenta_archivos(origen, "*.pdf") + _cuenta_archivos(origen, "*.md")
        fragmentos = _cuenta_archivos(preparado, "*.md")
        estado_kb = binding.knowledge_base_id or c.apagado("sin KB asignada")
        print(f"  {c.negrita(slug)}  {c.apagado(binding.profile.name)}")
        c.tabla(
            [
                ("originales", f"{originales} en {binding.source_dir or '—'}"),
                ("preparados", f"{fragmentos} en {binding.prepared_dir or '—'}"),
                ("knowledge base", str(estado_kb)),
            ],
            sangria="      ",
        )
        if originales and not fragmentos:
            pendientes.append(f"Preparar el corpus de '{slug}' (opción 3)")
        if fragmentos and not binding.knowledge_base_id:
            pendientes.append(f"Desplegar e ingerir '{slug}' (opciones 5 y 6)")
        print()

    if pendientes:
        print(f"  {c.negrita('Pendiente')}")
        for tarea in dict.fromkeys(pendientes):
            print(f"    · {tarea}")
    else:
        c.exito("Todo lo configurable está configurado.")


def _ejecutar(comando: list[str], *, entorno: dict[str, str] | None = None) -> None:
    print()
    c.paso(" ".join(comando))
    print()
    env = {**os.environ, **(entorno or {})}
    try:
        codigo = subprocess.call(comando, cwd=RAIZ, env=env)
    except FileNotFoundError:
        c.error(f"No se encontró el ejecutable: {comando[0]}")
        return
    print()
    if codigo == 0:
        c.exito("Terminado")
    else:
        c.error(f"Terminó con código {codigo}")


# --- bucle principal ----------------------------------------------------------

ACCIONES = {
    "init": accion_init,
    "perfiles": accion_perfiles,
    "corpus": accion_corpus,
    "chat": accion_chat,
    "desplegar": accion_desplegar,
    "sync": accion_sync,
    "evaluar": accion_evaluar,
    "estado": accion_estado,
}


def _cabecera(estado: Estado) -> None:
    proyecto = estado.proyecto or c.rojo("sin configurar")
    tema = estado.perfil_activo or c.rojo("sin tema")
    backends = "/".join(estado.backends)
    print(f"\n{c.negrita('  Agente RAG')}  {c.apagado('·')}  {proyecto}  {c.apagado('·')}  tema: {c.cian(tema)}  {c.apagado(f'· {backends}')}")
    print(c.apagado("  " + "─" * 68))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ACCIONES:
        # `make init` entra por aquí: la misma acción, sin pasar por el menú.
        try:
            ACCIONES[argv[0]](cargar_estado())
        except c.Cancelado:
            return 130
        return 0
    if not c.interactivo():
        print("El menú necesita una terminal interactiva. Usa `make help` para los comandos sueltos.", file=sys.stderr)
        return 2

    while True:
        estado = cargar_estado()
        _cabecera(estado)
        opciones = [
            c.Opcion("init", "Inicializar el proyecto", "nombres, cuenta AWS y primer tema"),
            c.Opcion("perfiles", "Temas", "crear, activar o revisar un tema"),
            c.Opcion("corpus", "Preparar corpus", "documentos → fragmentos indexables"),
            c.Opcion("chat", "Probar en local", "conversar con el agente sin AWS"),
            c.Opcion("desplegar", "Desplegar", "build, push e infraestructura"),
            c.Opcion("sync", "Sincronizar la base de conocimiento", "sube el corpus y lanza la ingesta"),
            c.Opcion("evaluar", "Evaluar", "preguntas de oro y reporte"),
            c.Opcion("estado", "Estado", "qué está hecho y qué falta"),
            c.Opcion("salir", "Salir"),
        ]
        try:
            elegida = c.elegir(opciones)
            if elegida == "salir":
                print()
                return 0
            ACCIONES[elegida](estado)
        except c.Cancelado:
            print()
            return 0
        except Exception as exc:  # noqa: BLE001 - el menú nunca debe morir por una acción
            c.error(f"{type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
