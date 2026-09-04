#!/usr/bin/env bash
# Gemelo de `sync-kb.sh` para máquinas sin Terraform ni el repo completo.
#
# Aquel resuelve bucket, Knowledge Base y origen de datos preguntándole a
# Terraform, y el veto de marcadores preguntándole al perfil. Este no tiene ni
# lo uno ni lo otro: recibe todo por entorno. A cambio se lleva a otro servidor
# como archivo suelto.
#
#   RAG_S3_BUCKET=... RAG_KB_ID=... RAG_DS_ID=... ./sync-kb-remoto.sh finanzas ./corpus-preparado
#   RAG_ENV_FILE=~/rag-finanzas.env ./sync-kb-remoto.sh
#
# Variables (obligatorias salvo donde se indique):
#
#   RAG_TEMA                  slug del tema; es también el prefijo en S3.
#                             También se puede pasar como argumento 1.
#   RAG_CORPUS_DIR            carpeta del corpus PREPARADO (.md + .metadata.json).
#                             También como argumento 2. Nunca los originales.
#   RAG_S3_BUCKET             bucket del corpus.
#   RAG_KB_ID                 Knowledge Base del tema.
#   RAG_DS_ID                 origen de datos del tema.
#   AWS_REGION                opcional; us-east-1 por defecto.
#   AWS_PROFILE               opcional; el perfil de credenciales a usar.
#   RAG_AWS_ACCOUNT_ID        opcional pero muy recomendable: si se declara, se
#                             aborta cuando las credenciales activas apuntan a
#                             otra cuenta.
#   RAG_MARCADORES_VETADOS    opcional; ERE (alternativas con `|`) que no puede
#                             aparecer en el corpus. Equivale a `banned_markers`
#                             del perfil, que aquí no se puede leer.
#   RAG_ENV_FILE              opcional; archivo con estas variables en formato
#                             `CLAVE=valor` que se carga antes que nada.
#
# Para generar ese archivo desde la máquina que sí tiene el estado:
#
#   TEMA=finanzas
#   { echo "RAG_TEMA=$TEMA"
#     echo "RAG_AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)"
#     echo "RAG_S3_BUCKET=$(terraform -chdir=infra output -raw corpus_bucket)"
#     echo "RAG_KB_ID=$(terraform -chdir=infra output -json knowledge_base_ids | jq -r .\"$TEMA\")"
#     echo "RAG_DS_ID=$(terraform -chdir=infra output -json data_source_ids   | jq -r .\"$TEMA\")"
#   } > rag-$TEMA.env
set -euo pipefail

fallo() { echo "❌ $1" >&2; exit 1; }

if [[ -n "${RAG_ENV_FILE:-}" ]]; then
  [[ -f "$RAG_ENV_FILE" ]] || fallo "No existe RAG_ENV_FILE: $RAG_ENV_FILE"
  set -a; . "$RAG_ENV_FILE"; set +a
fi

TEMA="${1:-${RAG_TEMA:-}}"
CORPUS="${2:-${RAG_CORPUS_DIR:-}}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

for var in TEMA CORPUS RAG_S3_BUCKET RAG_KB_ID RAG_DS_ID; do
  [[ -n "${!var:-}" ]] || fallo "Falta $var. Ver la cabecera de este script."
done

CORPUS="${CORPUS/#\~/$HOME}"
[[ -d "$CORPUS" ]] || fallo "No existe la carpeta de corpus preparado: $CORPUS"

# El corpus preparado son pares .md + .metadata.json. Una carpeta sin .md casi
# siempre significa que se apuntó a los originales por error.
compgen -G "$CORPUS"/*.md >/dev/null \
  || fallo "En $CORPUS no hay ningún .md. ¿Es la carpeta del corpus preparado?"

# Guarda de contenido: lo vetado no llega a S3 ni por error. Es la última red
# antes de que el agente pueda recitarlo.
if [[ -n "${RAG_MARCADORES_VETADOS:-}" ]] \
   && grep -rliE "$RAG_MARCADORES_VETADOS" "$CORPUS" >/dev/null 2>&1; then
  fallo "Hay material vetado en $CORPUS. Sacarlo antes de sincronizar."
fi

# Guarda de cuenta. Sin Terraform delante, esta es la única barrera entre un
# sync y el bucket de otro despliegue.
CUENTA_REAL="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || fallo "No hay sesión de AWS válida${AWS_PROFILE:+ para el perfil '$AWS_PROFILE'}."
if [[ -n "${RAG_AWS_ACCOUNT_ID:-}" && "$CUENTA_REAL" != "$RAG_AWS_ACCOUNT_ID" ]]; then
  fallo "Las credenciales apuntan a $CUENTA_REAL y se esperaba $RAG_AWS_ACCOUNT_ID. Abortado."
fi

echo "▸ Subiendo $CORPUS → s3://$RAG_S3_BUCKET/$TEMA/ (cuenta $CUENTA_REAL)"
aws s3 sync "$CORPUS" "s3://$RAG_S3_BUCKET/$TEMA/" \
  --delete \
  --exclude "*" --include "*.md" --include "*.metadata.json" \
  --only-show-errors
aws s3 ls "s3://$RAG_S3_BUCKET/$TEMA/" --recursive --summarize | tail -2

echo "▸ Lanzando la ingesta de '$TEMA'"
JOB="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "$RAG_KB_ID" --data-source-id "$RAG_DS_ID" \
  --query 'ingestionJob.ingestionJobId' --output text)"

echo "  job $JOB"
while true; do
  ESTADO="$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "$RAG_KB_ID" --data-source-id "$RAG_DS_ID" --ingestion-job-id "$JOB" \
    --query 'ingestionJob.status' --output text)"
  case "$ESTADO" in
    COMPLETE) break ;;
    FAILED|STOPPED)
      aws bedrock-agent get-ingestion-job \
        --knowledge-base-id "$RAG_KB_ID" --data-source-id "$RAG_DS_ID" \
        --ingestion-job-id "$JOB" --query 'ingestionJob.failureReasons' --output text
      fallo "La ingesta terminó en estado $ESTADO" ;;
    *) printf '  %s…\r' "$ESTADO"; sleep 5 ;;
  esac
done

echo "▸ Resultado de la ingesta"
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "$RAG_KB_ID" --data-source-id "$RAG_DS_ID" --ingestion-job-id "$JOB" \
  --query 'ingestionJob.statistics' --output table

# El modo de falla silencioso de una KB es ingerir cero fragmentos y responder
# "no consta" a todo. Se verifica aquí, no en la consola.
INDEXADOS="$(aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "$RAG_KB_ID" --data-source-id "$RAG_DS_ID" --ingestion-job-id "$JOB" \
  --query 'ingestionJob.statistics.numberOfNewDocumentsIndexed' --output text)"
[[ "$INDEXADOS" != "0" ]] || fallo "La ingesta no indexó ningún documento. Revisar formato y metadatos."

echo
echo "✅ $INDEXADOS fragmento(s) indexados para '$TEMA' en la KB $RAG_KB_ID"
