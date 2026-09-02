"""Asistente de inicialización: de repositorio clonado a proyecto configurado.

Nada del nombre del proyecto, la cuenta de AWS o el tema está escrito en el
código: se pregunta aquí una vez y se escribe en los tres archivos que mandan.

    .env                      → cómo corre el servicio (backends, token, perfil)
    infra/terraform.tfvars    → dónde y con qué nombre se despliega
    profiles/<slug>.yaml      → de qué habla el agente y con qué reglas

Los tres son archivos generados y los tres están fuera de git salvo el perfil,
que sí se versiona: las reglas del agente son código, la cuenta de AWS no.

Todo valor existente se ofrece como defecto. Reconfigurar para cambiar la
región no debería obligar a reescribir el tema.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import console as c

PLANTILLA_PERFIL = """\
# Perfil: {name}
#
# Generado por `make init`. Es un archivo normal: edítalo a mano cuando quieras
# afinar las reglas — el servicio lo relee al arrancar, sin redesplegar nada.

slug: {slug}
name: {name}

# Las dos frases que sitúan al modelo: sobre qué responde y con qué material.
# Son lo primero que conviene afinar si las respuestas no salen como esperas.
subject: >-
  {subject}
sources: {sources}

# Qué dice exactamente cuando la evidencia no alcanza. Que sea una frase fija
# es lo que permite medir cuántas veces declina.
decline_phrase: "{decline}"

# Reglas propias de este tema. Se aplican DESPUÉS de las innegociables
# (fundamento documental, cita obligatoria, negación explícita), así que pueden
# añadir postura o formato pero nunca relajar el fundamento.
extra_rules:{extra_rules}

# Identificadores a enmascarar en la salida: curp, rfc, telefono, cedula, email.
# Lista vacía = no enmascarar nada. Cuidado con `cedula` en corpus técnicos:
# tapa cualquier número de 7-8 cifras, incluidos precios y potencias.
redaction: [{redaction}]

retrieval:
  top_k: {top_k}
  # Piso de relevancia. Con corpus pequeños déjalo en 0.0: las bandas de score
  # se solapan y cualquier piso tumba preguntas legítimas.
  min_score: {min_score}

chunking:
  max_chars: {max_chars}
  overlap_chars: {overlap}
  # Por debajo de esto el documento no se trocea y se cita entero.
  min_chars_to_split: {min_split}

# Tramos de la ruta que se convierten en metadatos. Con `path_metadata: [marca]`
# y `origen/toyota/hilux.pdf`, el fragmento sale con `marca=toyota`.
path_metadata: [{path_metadata}]

# Marcadores que impiden indexar un documento. Se buscan en su texto.
banned_markers:{banned}

corpus:
  source: {source}
  prepared: {prepared}
"""


@dataclass
class Proyecto:
    nombre: str
    entorno: str
    aws_profile: str
    aws_account: str
    aws_region: str
    api_token: str
    default_model: str


# --- lectura y escritura de los archivos generados ---------------------------

_ENV = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")
_TFVAR = re.compile(r'^\s*([a-z0-9_]+)\s*=\s*"?([^"]*)"?\s*$')


def leer_pares(ruta: Path, patron: re.Pattern[str]) -> dict[str, str]:
    if not ruta.is_file():
        return {}
    valores: dict[str, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        if linea.lstrip().startswith("#"):
            continue
        if (m := patron.match(linea)):
            valores[m.group(1)] = m.group(2)
    return valores


def escribir_env(ruta: Path, proyecto: Proyecto, perfil_activo: str) -> None:
    previo = leer_pares(ruta, _ENV)
    contenido = f"""\
# Generado por `make init`. Editable a mano; se regenera al reinicializar.
# NUNCA se commitea: lleva el token del entorno local.
RAG_ENVIRONMENT=local
RAG_LOG_LEVEL={previo.get('RAG_LOG_LEVEL', 'INFO')}
RAG_API_TOKEN={proyecto.api_token}
RAG_DEFAULT_MODEL={proyecto.default_model}

# Tema activo en local. El desplegado sirve todos los de profiles/ a la vez y
# el cliente elige con la cabecera `X-Rag-Profile`.
RAG_PROFILES_DIR=profiles
RAG_DEFAULT_PROFILE={perfil_activo}

# Backends. `local`/`stub` no tocan AWS: sirven para desarrollar sin gastar.
RAG_RETRIEVAL_BACKEND={previo.get('RAG_RETRIEVAL_BACKEND', 'local')}
RAG_INFERENCE_BACKEND={previo.get('RAG_INFERENCE_BACKEND', 'stub')}

# AWS (solo con backends bedrock)
RAG_AWS_PROFILE={proyecto.aws_profile}
RAG_AWS_REGION={proyecto.aws_region}
"""
    ruta.write_text(contenido, encoding="utf-8")


def escribir_tfvars(ruta: Path, proyecto: Proyecto) -> None:
    previo = leer_pares(ruta, _TFVAR)
    conservados = "".join(
        f'{clave:<16}= "{valor}"\n'
        for clave, valor in previo.items()
        if clave in ("certificate_arn", "guardrail_id") and valor
    )
    ruta.write_text(
        f"""\
# Generado por `make init`. No se commitea: lleva el ID de cuenta.
project        = "{proyecto.nombre}"
environment    = "{proyecto.entorno}"
aws_account_id = "{proyecto.aws_account}"
aws_profile    = "{proyecto.aws_profile}"
aws_region     = "{proyecto.aws_region}"
{conservados}
# Sin certificado el ALB sirve HTTP en claro: vale para verificar el despliegue,
# no para entregar. Poner el ARN de ACM antes de exponer el endpoint de verdad.
# certificate_arn = "arn:aws:acm:{proyecto.aws_region}:{proyecto.aws_account}:certificate/..."

# Restringir en cuanto se sepa desde dónde se va a consumir.
# allowed_cidrs = ["203.0.113.0/24"]
""",
        encoding="utf-8",
    )


def cuenta_del_perfil(perfil: str) -> str | None:
    """Pregunta a AWS en vez de hacérselo teclear. Silencioso si no hay sesión."""
    try:
        salida = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--profile", perfil, "--query", "Account", "--output", "text"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    return salida if re.fullmatch(r"\d{12}", salida) else None


# --- asistentes ---------------------------------------------------------------


def preguntar_proyecto(raiz: Path) -> Proyecto:
    env = leer_pares(raiz / ".env", _ENV)
    tf = leer_pares(raiz / "infra" / "terraform.tfvars", _TFVAR)

    c.titulo("Proyecto")
    nombre = c.slugificar(
        c.preguntar("Nombre del proyecto", defecto=tf.get("project", "rag-agent"))
    )
    entorno = c.preguntar("Entorno", defecto=tf.get("environment", "prod"))

    c.titulo("AWS")
    print(c.apagado("  Solo se usa al desplegar. En local el servicio no toca AWS.\n"))
    aws_profile = c.preguntar("Perfil de credenciales", defecto=tf.get("aws_profile", env.get("RAG_AWS_PROFILE", "default")))
    detectada = cuenta_del_perfil(aws_profile)
    if detectada:
        c.exito(f"Cuenta detectada con ese perfil: {detectada}")
    cuenta = c.preguntar(
        "ID de cuenta (12 dígitos)", defecto=detectada or tf.get("aws_account_id", ""), obligatorio=False
    )
    while cuenta and not re.fullmatch(r"\d{12}", cuenta):
        c.error("El ID de cuenta son 12 dígitos.")
        cuenta = c.preguntar("ID de cuenta (12 dígitos)", defecto="", obligatorio=False)
    region = c.preguntar("Región", defecto=tf.get("aws_region", env.get("RAG_AWS_REGION", "us-east-1")))

    c.titulo("Servicio")
    token = c.preguntar(
        "Token de API para desarrollo local", defecto=env.get("RAG_API_TOKEN", "local-dev-token")
    )
    modelo = c.preguntar("Alias de modelo por defecto", defecto=env.get("RAG_DEFAULT_MODEL", "agente-rag-sonnet"))

    return Proyecto(
        nombre=nombre,
        entorno=entorno,
        aws_profile=aws_profile,
        aws_account=cuenta,
        aws_region=region,
        api_token=token,
        default_model=modelo,
    )


def preguntar_perfil(raiz: Path, *, existentes: tuple[str, ...] = ()) -> Path:
    """Crea un `profiles/<slug>.yaml` a partir de unas pocas preguntas."""
    c.titulo("Tema del RAG")
    print(c.apagado("  Un tema = un corpus + sus reglas. Un despliegue sirve varios a la vez.\n"))

    nombre = c.preguntar("Nombre del tema", defecto="Marcas de coches")
    slug = c.slugificar(c.preguntar("Identificador (slug)", defecto=c.slugificar(nombre)))
    while slug in existentes and not c.confirmar(f"Ya existe el perfil '{slug}'. ¿Sobrescribirlo?", defecto=False):
        slug = c.slugificar(c.preguntar("Identificador (slug)", defecto=""))

    print()
    print(c.apagado("  Completa la frase: «Eres un agente que responde preguntas sobre…»"))
    subject = c.preguntar("  sobre", defecto="los vehículos y especificaciones de las marcas documentadas")
    print(c.apagado("  …«apoyándote únicamente en…»"))
    sources = c.preguntar("  apoyándote en", defecto="los folletos y fichas técnicas oficiales")

    decline = c.preguntar(
        "Frase al declinar", defecto="Eso no consta en la documentación disponible."
    )

    c.titulo("Corpus")
    source = c.preguntar("Carpeta con los documentos originales", defecto=f"corpus/{slug}")
    prepared = c.preguntar("Carpeta del corpus preparado", defecto=f".corpus-preparado/{slug}")
    ruta_origen = (raiz / source).expanduser() if not source.startswith(("/", "~")) else Path(source).expanduser()
    if ruta_origen.is_dir():
        pdfs = len(list(ruta_origen.rglob("*.pdf")))
        subcarpetas = sorted({p.parent.name for p in ruta_origen.rglob("*.pdf") if p.parent != ruta_origen})
        c.exito(f"{pdfs} PDF encontrados" + (f" en {len(subcarpetas)} subcarpetas" if subcarpetas else ""))
        if subcarpetas:
            print(c.apagado(f"    {', '.join(subcarpetas[:8])}{'…' if len(subcarpetas) > 8 else ''}"))
    else:
        c.aviso(f"Todavía no existe {ruta_origen}. Puedes crearla después.")

    etiqueta = ""
    if c.confirmar("¿Las subcarpetas del origen son una categoría (marca, cliente, año…)?", defecto=bool(source)):
        etiqueta = c.slugificar(c.preguntar("Nombre del metadato", defecto="categoria"))

    c.titulo("Sensibilidad")
    sensible = c.confirmar("¿El corpus contiene datos personales que haya que enmascarar?", defecto=False)
    redaction = "curp, rfc, telefono, cedula" if sensible else ""
    if sensible:
        print(c.apagado("    El corpus preparado deberá vivir FUERA del repositorio."))
        prepared = c.preguntar("Carpeta del corpus preparado (fuera del repo)", defecto=f"~/corpus-{slug}")

    largos = c.confirmar("¿Los documentos son largos (folletos, informes, manuales)?", defecto=True)
    top_k, max_chars, min_split = (8, 2000, 2600) if largos else (6, 2000, 6000)
    min_score = "0.35" if largos else "0.0"

    contenido = PLANTILLA_PERFIL.format(
        slug=slug,
        name=nombre,
        subject=subject,
        sources=sources,
        decline=decline,
        extra_rules="\n  - >-\n    Escribe aquí una regla propia de este tema, o borra este bloque.",
        redaction=redaction,
        top_k=top_k,
        min_score=min_score,
        max_chars=max_chars,
        overlap=250 if largos else 200,
        min_split=min_split,
        path_metadata=etiqueta,
        banned=" []" if not sensible else "\n  - CREDENCIAL PARA VOTAR\n  - CLAVE DE ELECTOR\n  - PASAPORTE\n  - LICENCIA DE CONDUCIR",
        source=source,
        prepared=prepared,
    )
    destino = raiz / "profiles" / f"{slug}.yaml"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(contenido, encoding="utf-8")
    return destino


def ejecutar(raiz: Path, *, existentes: tuple[str, ...] = ()) -> str:
    """Asistente completo. Devuelve el slug del tema configurado."""
    print(c.negrita("\n  Configuración del proyecto"))
    print(c.apagado("  Se escribirán .env, infra/terraform.tfvars y profiles/<tema>.yaml.\n"))

    proyecto = preguntar_proyecto(raiz)
    perfil = preguntar_perfil(raiz, existentes=existentes)
    slug = perfil.stem

    escribir_env(raiz / ".env", proyecto, slug)
    (raiz / "infra").mkdir(exist_ok=True)
    escribir_tfvars(raiz / "infra" / "terraform.tfvars", proyecto)

    c.titulo("Listo")
    c.tabla(
        [
            (".env", "backends, token y tema activo"),
            ("infra/terraform.tfvars", f"{proyecto.nombre}-{proyecto.entorno} en {proyecto.aws_region}"),
            (str(perfil.relative_to(raiz)), "reglas y corpus del tema"),
        ]
    )
    return slug
