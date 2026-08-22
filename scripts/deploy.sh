#!/usr/bin/env bash
# Construye, publica y despliega. La guarda de cuenta va primero: un apply en
# la cuenta equivocada se limpia a mano, y a veces no del todo.
#
#   ./scripts/deploy.sh            # build + push + apply
#   ./scripts/deploy.sh plan       # solo plan
#   ./scripts/deploy.sh bootstrap  # solo crear el ECR (primer despliegue)
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$RAIZ/infra"
ACCION="${1:-apply}"

fallo() { echo "❌ $1" >&2; exit 1; }

[[ -f "$INFRA/terraform.tfvars" ]] || fallo "Falta infra/terraform.tfvars (copiar de terraform.tfvars.example)"

# BSD sed (macOS) no entiende \s: se usan clases POSIX para que el script
# funcione igual en la Mac del desarrollo y en el Linux del CI.
leer_var() {
  grep -E "^[[:space:]]*$1[[:space:]]*=" "$INFRA/terraform.tfvars" \
    | head -1 \
    | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'
}

CUENTA_ESPERADA="$(leer_var aws_account_id)"
PERFIL="$(leer_var aws_profile)"; PERFIL="${PERFIL:-luis}"
REGION="$(leer_var aws_region)"; REGION="${REGION:-us-east-1}"

[[ -n "$CUENTA_ESPERADA" ]] || fallo "aws_account_id no está definido en terraform.tfvars"

# --- Guarda de cuenta y de sesión --------------------------------------------
CUENTA_REAL="$(aws sts get-caller-identity --profile "$PERFIL" --query Account --output text 2>/dev/null)" \
  || fallo "El perfil '$PERFIL' no tiene sesión válida. Si es SSO: aws sso login --profile $PERFIL"

if [[ "$CUENTA_REAL" != "$CUENTA_ESPERADA" ]]; then
  fallo "El perfil '$PERFIL' apunta a la cuenta $CUENTA_REAL y terraform.tfvars declara $CUENTA_ESPERADA. Abortado."
fi
echo "▸ Cuenta $CUENTA_REAL verificada con el perfil $PERFIL ($REGION)"

export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION"
terraform -chdir="$INFRA" init -input=false >/dev/null

# `terraform output -raw` con estado vacío NO falla: devuelve 0 y escribe la
# advertencia "No outputs found" en la salida estándar. Comprobar el código de
# retorno es insuficiente — la advertencia acabaría usada como si fuera el
# valor. Se valida la forma del dato.
tf_output() {
  local valor
  valor="$(terraform -chdir="$INFRA" output -raw "$1" 2>/dev/null)" || return 1
  [[ -n "$valor" && "$valor" != *"No outputs"* && "$valor" != *$'\n'* ]] || return 1
  printf '%s' "$valor"
}

if [[ "$ACCION" == "plan" ]]; then
  terraform -chdir="$INFRA" plan
  exit 0
fi

# --- Primer despliegue: el ECR debe existir antes de poder empujar la imagen --
ECR="$(tf_output ecr_repository_url || true)"

if [[ "$ACCION" == "bootstrap" || -z "$ECR" ]]; then
  echo "▸ Creando el repositorio de imágenes"
  terraform -chdir="$INFRA" apply -input=false -auto-approve -target=aws_ecr_repository.api
  ECR="$(tf_output ecr_repository_url || true)"
  [[ "$ACCION" == "bootstrap" ]] && exit 0
fi

[[ "$ECR" =~ ^[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/ ]] \
  || fallo "La URL del ECR no tiene forma válida: '${ECR:0:80}'"
TAG="$(git -C "$RAIZ" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"

echo "▸ Construyendo la imagen ($TAG)"
# --platform explícito: Fargate corre x86_64 y una Mac con Apple Silicon
# construiría arm64 sin avisar, produciendo tareas que mueren al arrancar.
docker build --platform linux/amd64 -t "$ECR:$TAG" -t "$ECR:bootstrap" "$RAIZ"

echo "▸ Publicando en ECR"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ECR%%/*}"
docker push "$ECR:$TAG"
docker push "$ECR:bootstrap"

echo "▸ Aplicando la infraestructura"
terraform -chdir="$INFRA" apply -input=false -auto-approve -var "container_image=$ECR:$TAG"

BASE="$(tf_output base_url)"
SECRETO="$(tf_output api_token_secret_arn)"
echo
echo "✅ Desplegado en $BASE"
echo
echo "Para verificarlo:"
echo "  export BASE_URL=$BASE"
echo "  export API_TOKEN=\$(aws secretsmanager get-secret-value --secret-id $SECRETO --query SecretString --output text)"
echo "  make smoke && make test-deployed"
