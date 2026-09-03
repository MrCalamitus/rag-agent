#!/usr/bin/env bash
# Sube los documentos ORIGINALES de un tema al prefijo `originales/` de S3.
#
#   ./scripts/sync-originales.sh autos
#   make sync-originales PROFILE=autos
#
# Es el gemelo de `sync-kb.sh` y su contrario deliberado. Aquel sube el corpus
# preparado, que es lo que el agente puede recitar; este sube los archivos tal
# cual, que es lo que un lector puede abrir. Van a prefijos distintos y este
# **no** es origen de datos de ninguna Knowledge Base: nada de lo que se suba
# aquí entra al índice.
#
# Solo sube lo que el perfil autoriza. Un tema sin `documentos.expone` no sube
# nada, y dentro de un tema solo viajan los archivos cuya clase esté expuesta.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$RAIZ/infra"
PERFIL_TEMA="${1:-${RAG_PROFILE:-}}"

fallo() { echo "❌ $1" >&2; exit 1; }

[[ -n "$PERFIL_TEMA" ]] || fallo "Uso: ./scripts/sync-originales.sh <tema>"

cd "$RAIZ"

# Qué archivos autoriza el perfil. Se le pregunta al mismo código que decide en
# tiempo de respuesta, para que no puedan discrepar.
LISTA="$("$RAIZ/.venv/bin/python" - "$RAIZ" "$PERFIL_TEMA" <<'PYLISTA'
import sys
from pathlib import Path

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "src"))
from rag_agent.infrastructure.profiles import load_profiles

binding = load_profiles(raiz / "profiles").get(sys.argv[2])
if binding is None:
    raise SystemExit(f"perfil desconocido: {sys.argv[2]}")

politica = binding.profile.documents
if not politica.expone_algo:
    raise SystemExit(
        f"El tema '{binding.slug}' no expone ningún documento "
        "(documentos.expone está vacío). Nada que subir."
    )

origen = Path((binding.source_dir or "").replace("~", str(Path.home())))
if not origen.is_dir():
    raise SystemExit(f"No existe la carpeta de originales: {origen}")

for ruta in sorted(origen.rglob("*")):
    if not ruta.is_file() or ruta.name.endswith(".metadata.json"):
        continue
    # La clase se recalcula con las mismas señales que en la ingesta. El texto no
    # se lee aquí —sería releer el corpus entero—, así que los marcadores de
    # contenido no participan: por eso un tema que dependa de ellos debe
    # apoyarse además en la ruta.
    clase = politica.clasificar(
        ruta=ruta.relative_to(origen).as_posix(),
        tipo="",
        texto="",
    )
    if politica.expuesta(clase):
        print(ruta)
PYLISTA
)" || fallo "No se pudo determinar qué documentos expone '$PERFIL_TEMA'"

[[ -n "$LISTA" ]] || fallo "El tema '$PERFIL_TEMA' no tiene documentos que exponer."

leer_var() {
  grep -E "^[[:space:]]*$1[[:space:]]*=" "$INFRA/terraform.tfvars" \
    | head -1 | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'
}

CUENTA_ESPERADA="$(leer_var aws_account_id)"
PERFIL_AWS="$(leer_var aws_profile)"; PERFIL_AWS="${PERFIL_AWS:-default}"

CUENTA_REAL="$(aws sts get-caller-identity --profile "$PERFIL_AWS" --query Account --output text 2>/dev/null)" \
  || fallo "El perfil '$PERFIL_AWS' no tiene sesión válida."
[[ "$CUENTA_REAL" == "$CUENTA_ESPERADA" ]] \
  || fallo "Cuenta equivocada: esperada $CUENTA_ESPERADA, activa $CUENTA_REAL."

BUCKET="$(terraform -chdir=infra output -raw corpus_bucket)"
DESTINO="s3://$BUCKET/originales/$PERFIL_TEMA/"

TOTAL="$(wc -l <<< "$LISTA" | tr -d ' ')"
echo "▸ $TOTAL documento(s) autorizados → $DESTINO"

# Aplanado a propósito: la metadata guarda `fuente` como nombre de archivo, no
# como ruta, y es por ese nombre por el que el servicio los pide.
while IFS= read -r ruta; do
  [[ -n "$ruta" ]] || continue
  aws s3 cp --profile "$PERFIL_AWS" --only-show-errors "$ruta" "$DESTINO$(basename "$ruta")"
done <<< "$LISTA"

echo "✅ Originales de '$PERFIL_TEMA' sincronizados"
echo "   Falta que el servicio sepa el bucket: RAG_DOCUMENTS_BUCKET=$BUCKET"
