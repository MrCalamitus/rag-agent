#!/usr/bin/env bash
# Sube el corpus preparado de un tema a su prefijo en S3 y lanza su ingesta.
#
#   ./scripts/sync-kb.sh coches                 # usa la carpeta del perfil
#   ./scripts/sync-kb.sh coches /ruta/al/corpus # o una explícita
#   make sync-kb PROFILE=coches
#
# Sube SOLO la carpeta del corpus preparado, nunca la de documentos originales:
# lo que entra a S3 es lo que el agente puede llegar a recitar.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$RAIZ/infra"
PERFIL_TEMA="${1:-${RAG_PROFILE:-}}"
CORPUS="${2:-}"

fallo() { echo "❌ $1" >&2; exit 1; }

[[ -n "$PERFIL_TEMA" ]] || fallo "Uso: ./scripts/sync-kb.sh <tema> [carpeta-del-corpus]"

# El perfil sabe dónde vive su corpus y qué marcadores prohíbe. Se le pregunta a
# él en vez de repetir esos datos aquí: una sola fuente de verdad.
datos_perfil() {
  "$RAIZ/.venv/bin/python" - "$RAIZ" "$PERFIL_TEMA" "$1" <<'PYPERFIL'
import sys
from pathlib import Path

raiz = Path(sys.argv[1])
sys.path.insert(0, str(raiz / "src"))
from rag_agent.infrastructure.profiles import load_profiles

slug, campo = sys.argv[2], sys.argv[3]
binding = load_profiles(raiz / "profiles").get(slug)
if binding is None:
    raise SystemExit(f"perfil desconocido: {slug}")
print({"prepared": binding.prepared_dir or "", "banned": "|".join(binding.profile.banned_markers)}[campo])
PYPERFIL
}

cd "$RAIZ"
if [[ -z "$CORPUS" ]]; then
  CORPUS="$(datos_perfil prepared)" || fallo "No se pudo leer el perfil '$PERFIL_TEMA'"
  CORPUS="${CORPUS/#\~/$HOME}"
fi
[[ -n "$CORPUS" ]] || fallo "El perfil '$PERFIL_TEMA' no declara carpeta de corpus preparado."
[[ -d "$CORPUS" ]] || fallo "No existe la carpeta: $CORPUS. ¿Ejecutaste 'make corpus PROFILE=$PERFIL_TEMA'?"

leer_var() {
  grep -E "^[[:space:]]*$1[[:space:]]*=" "$INFRA/terraform.tfvars" \
    | head -1 \
    | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'
}

CUENTA_ESPERADA="$(leer_var aws_account_id)"
PERFIL_AWS="$(leer_var aws_profile)"; PERFIL_AWS="${PERFIL_AWS:-default}"
REGION="$(leer_var aws_region)"; REGION="${REGION:-us-east-1}"

CUENTA_REAL="$(aws sts get-caller-identity --profile "$PERFIL_AWS" --query Account --output text 2>/dev/null)" \
  || fallo "El perfil '$PERFIL_AWS' no tiene sesión válida."
[[ "$CUENTA_REAL" == "$CUENTA_ESPERADA" ]] \
  || fallo "El perfil apunta a $CUENTA_REAL y terraform.tfvars declara $CUENTA_ESPERADA. Abortado."

export AWS_PROFILE="$PERFIL_AWS" AWS_REGION="$REGION"

BUCKET="$(terraform -chdir="$INFRA" output -raw corpus_bucket)"
KB="$(terraform -chdir="$INFRA" output -json knowledge_base_ids | "$RAIZ/.venv/bin/python" -c "import json,sys;print(json.load(sys.stdin).get('$PERFIL_TEMA',''))")"
DS="$(terraform -chdir="$INFRA" output -json data_source_ids | "$RAIZ/.venv/bin/python" -c "import json,sys;print(json.load(sys.stdin).get('$PERFIL_TEMA',''))")"

[[ -n "$KB" && -n "$DS" ]] \
  || fallo "El tema '$PERFIL_TEMA' no tiene Knowledge Base desplegada. Ejecuta 'make deploy' primero."

# Guarda de contenido: lo que el perfil prohíbe indexar no llega a S3 ni por
# error. Es la última red antes de que el agente pueda recitarlo.
VETADOS="$(datos_perfil banned)"
if [[ -n "$VETADOS" ]] && grep -rliE "$VETADOS" "$CORPUS" >/dev/null 2>&1; then
  fallo "Hay material vetado por el perfil '$PERFIL_TEMA' en $CORPUS. Sacarlo antes de sincronizar."
fi

echo "▸ Subiendo $CORPUS → s3://$BUCKET/$PERFIL_TEMA/"
aws s3 sync "$CORPUS" "s3://$BUCKET/$PERFIL_TEMA/" \
  --delete \
  --exclude "*" --include "*.md" --include "*.metadata.json" \
  --only-show-errors
aws s3 ls "s3://$BUCKET/$PERFIL_TEMA/" --recursive --summarize | tail -2

echo "▸ Lanzando la ingesta de '$PERFIL_TEMA'"
JOB="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "$KB" --data-source-id "$DS" \
  --query 'ingestionJob.ingestionJobId' --output text)"

echo "  job $JOB"
while true; do
  ESTADO="$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "$KB" --data-source-id "$DS" --ingestion-job-id "$JOB" \
    --query 'ingestionJob.status' --output text)"
  case "$ESTADO" in
    COMPLETE) break ;;
    FAILED|STOPPED)
      aws bedrock-agent get-ingestion-job --knowledge-base-id "$KB" --data-source-id "$DS" \
        --ingestion-job-id "$JOB" --query 'ingestionJob.failureReasons' --output text
      fallo "La ingesta terminó en estado $ESTADO" ;;
    *) printf '  %s…\r' "$ESTADO"; sleep 5 ;;
  esac
done

echo "▸ Resultado de la ingesta"
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "$KB" --data-source-id "$DS" --ingestion-job-id "$JOB" \
  --query 'ingestionJob.statistics' --output table

# El modo de falla silencioso de una KB es ingerir cero fragmentos y responder
# "no consta" a todo. Se verifica aquí, no en la consola.
INDEXADOS="$(aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "$KB" --data-source-id "$DS" --ingestion-job-id "$JOB" \
  --query 'ingestionJob.statistics.numberOfNewDocumentsIndexed' --output text)"
[[ "$INDEXADOS" != "0" ]] || fallo "La ingesta no indexó ningún documento. Revisar formato y metadatos."

echo
echo "✅ $INDEXADOS fragmento(s) indexados para '$PERFIL_TEMA' en la KB $KB"
echo "   Prueba: curl -H 'X-Rag-Profile: $PERFIL_TEMA' ..."
