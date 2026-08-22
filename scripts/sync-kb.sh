#!/usr/bin/env bash
# Sube el corpus preparado a S3 y lanza la ingesta a la Knowledge Base.
#
#   ./scripts/sync-kb.sh ~/docsLuis/corpus-luis-cv
#
# Sube SOLO la carpeta del corpus preparado, nunca la de documentos originales:
# lo que entra a S3 es lo que el agente puede llegar a recitar.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$RAIZ/infra"
CORPUS="${1:-${LUISCV_CORPUS_DIR:-}}"

fallo() { echo "❌ $1" >&2; exit 1; }

[[ -n "$CORPUS" ]] || fallo "Uso: ./scripts/sync-kb.sh <carpeta-del-corpus>"
[[ -d "$CORPUS" ]] || fallo "No existe la carpeta: $CORPUS"

leer_var() {
  grep -E "^[[:space:]]*$1[[:space:]]*=" "$INFRA/terraform.tfvars" \
    | head -1 \
    | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'
}

CUENTA_ESPERADA="$(leer_var aws_account_id)"
PERFIL="$(leer_var aws_profile)"; PERFIL="${PERFIL:-luis}"
REGION="$(leer_var aws_region)"; REGION="${REGION:-us-east-1}"

CUENTA_REAL="$(aws sts get-caller-identity --profile "$PERFIL" --query Account --output text 2>/dev/null)" \
  || fallo "El perfil '$PERFIL' no tiene sesión válida."
[[ "$CUENTA_REAL" == "$CUENTA_ESPERADA" ]] \
  || fallo "El perfil apunta a $CUENTA_REAL y terraform.tfvars declara $CUENTA_ESPERADA. Abortado."

export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION"

BUCKET="$(terraform -chdir="$INFRA" output -raw corpus_bucket)"
KB="$(terraform -chdir="$INFRA" output -raw knowledge_base_id)"
DS="$(terraform -chdir="$INFRA" output -raw data_source_id)"

# Guarda de contenido: un documento de identidad no entra al corpus ni por
# error. El agente no debe poder recitar un domicilio ni una clave de elector.
if grep -rliE "clave de elector|credencial para votar" "$CORPUS" >/dev/null 2>&1; then
  fallo "Hay un documento de identidad en $CORPUS. Sacarlo antes de sincronizar."
fi

echo "▸ Subiendo $CORPUS → s3://$BUCKET"
aws s3 sync "$CORPUS" "s3://$BUCKET/" \
  --delete \
  --exclude "*" --include "*.md" --include "*.metadata.json" \
  --only-show-errors
aws s3 ls "s3://$BUCKET/" --recursive --summarize | tail -2

echo "▸ Lanzando la ingesta"
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
echo "✅ $INDEXADOS documento(s) indexados en la KB $KB"
echo "   Prueba: BASE_URL=... API_TOKEN=... make smoke"
