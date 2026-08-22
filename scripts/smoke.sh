#!/usr/bin/env bash
# Verificación rápida contra el despliegue (contrato §10).
# Uso: BASE_URL=https://api.ejemplo API_TOKEN=... ./scripts/smoke.sh
set -euo pipefail

: "${BASE_URL:?define BASE_URL}"
: "${API_TOKEN:?define API_TOKEN}"
MODELO="${MODELO:-agente-rag-sonnet}"
BASE_URL="${BASE_URL%/}"

fallo() { echo "❌ $1"; exit 1; }

echo "▸ /healthz"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/healthz")" == "200" ]] || fallo "healthz no responde 200"

echo "▸ /readyz"
curl -sS "$BASE_URL/readyz" | grep -q '"status": *"ready"' || fallo "readyz no está listo"

echo "▸ 401 sin token"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/v1/responses" \
  -H 'Content-Type: application/json' -d '{"model":"'"$MODELO"'","input":"x"}')" == "401" ]] \
  || fallo "una petición sin token no fue rechazada"

echo "▸ camino feliz (no streaming)"
curl -sS -X POST "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"'"$MODELO"'","input":"¿Qué formación académica tiene?"}' \
  | tee /tmp/luis-cv-smoke.json | grep -q '"agente:knowledge_search"' \
  || fallo "la respuesta no trae el recibo de recuperación"

echo "▸ streaming con marcas de tiempo (B7 y B8)"
curl -sSN -X POST "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"'"$MODELO"'","stream":true,"input":"Resume su experiencia en la nube."}' \
  | while IFS= read -r linea; do printf '%s %s\n' "$(date +%s.%N)" "$linea"; done \
  | tee /tmp/luis-cv-stream.txt | tail -5

primero=$(grep -m1 'output_text.delta' /tmp/luis-cv-stream.txt | cut -d' ' -f1 || true)
ultimo=$(grep 'output_text.delta' /tmp/luis-cv-stream.txt | tail -1 | cut -d' ' -f1 || true)
[[ -n "$primero" && -n "$ultimo" ]] || fallo "no llegaron deltas"
separacion=$(awk -v a="$primero" -v b="$ultimo" 'BEGIN{printf "%.3f", b-a}')
echo "   deltas repartidos en ${separacion}s"
awk -v s="$separacion" 'BEGIN{exit !(s > 0.05)}' || fallo "los deltas llegaron juntos: hay buffering tras el ALB"

echo "▸ caso negativo (no debe inventar)"
curl -sS -X POST "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"'"$MODELO"'","input":"¿Tiene certificación CISSP?"}' \
  | grep -qi 'no consta' || fallo "no declinó ante una credencial inexistente"

echo "✅ smoke OK contra $BASE_URL"
