# Comandos del proyecto. `make help` lista lo disponible.
SHELL := /bin/bash
PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
IMAGE ?= rag-agent/api:local

.DEFAULT_GOAL := help

.PHONY: help
help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

.PHONY: install
install: .venv ## Crea el entorno e instala dependencias

.PHONY: menu
menu: .venv ## Menú interactivo: configurar, preparar, probar y desplegar
	$(PY) -m rag_agent.infrastructure.inbound.cli.menu

.PHONY: init
init: .venv ## Asistente de configuración: nombres, cuenta AWS y primer tema
	$(PY) -m rag_agent.infrastructure.inbound.cli.menu init

.PHONY: estado
estado: .venv ## Qué está configurado y qué falta
	$(PY) -m rag_agent.infrastructure.inbound.cli.menu estado

.PHONY: run
run: .venv ## Levanta la API en local (adaptadores locales, sin AWS)
	.venv/bin/uvicorn rag_agent.main:app --reload --port 8080

.PHONY: test
test: .venv ## Suite completa en local: contrato + RAG + operación
	$(PY) -m pytest -q

.PHONY: test-contract
test-contract: .venv ## Solo los casos A y B del contrato
	$(PY) -m pytest -q -m contract

.PHONY: test-rag
test-rag: .venv ## Solo los casos C: recuperación y veracidad
	$(PY) -m pytest -q -m rag

.PHONY: test-real
test-real: .venv ## Casos C contra el modelo real de Bedrock (usa .env)
	AWS_PROFILE=$${AWS_PROFILE:-luis} RAG_INFERENCE_BACKEND=bedrock $(PY) -m pytest -q tests/rag

.PHONY: test-deployed
test-deployed: .venv ## La misma suite contra el ALB (exige BASE_URL y API_TOKEN)
	@test -n "$$BASE_URL" || (echo "Define BASE_URL=https://... y API_TOKEN=..." && exit 1)
	$(PY) -m pytest -q -m deployed

.PHONY: eval
eval: .venv ## Preguntas de oro y reporte (PROFILE=tema, GOLDEN=ruta, MODELS=alias)
	$(PY) scripts/eval.py --models $${MODELS:-agente-rag-sonnet} \
		--golden $${GOLDEN:-tests/golden$${PROFILE:+-$$PROFILE}.yaml}

.PHONY: corpus
corpus: .venv ## Prepara el corpus de un tema (PROFILE=slug; SOURCE/OUT opcionales)
	@test -n "$$PROFILE" || (echo "Uso: make corpus PROFILE=autos" && exit 1)
	$(PY) scripts/prep_corpus.py --profile $$PROFILE --no-ocr\
		$${SOURCE:+--source $$SOURCE} $${OUT:+--out $$OUT} $${SKIP:+--skip $$SKIP} 

.PHONY: corpus-docling
corpus-docling: .venv ## Prepara el corpus con Docling, en paralelo al normal (PROFILE=slug; OCR=1)
	@test -n "$$PROFILE" || (echo "Uso: make corpus-docling PROFILE=autos [OCR=1]" && exit 1)
	@$(PY) -c "import docling" 2>/dev/null || \
		(echo "Falta docling: $(PIP) install -e \".[docling]\"" && exit 1)
	$(PY) scripts/prep_corpus_docling.py --profile $$PROFILE $${OCR:+--docling-ocr} \
		$${SOURCE:+--source $$SOURCE} $${OUT:+--out $$OUT} $${SKIP:+--skip $$SKIP} $${ONLY:+--only $$ONLY}

.PHONY: corpus-compare
corpus-compare: .venv ## Compara los dos corpus de un tema y mide cuál permite responder (PROFILE=slug)
	@test -n "$$PROFILE" || (echo "Uso: make corpus-compare PROFILE=autos" && exit 1)
	@test -n "$$A" -a -n "$$B" || (echo "Uso: make corpus-compare PROFILE=autos A=<dir> B=<dir>" && exit 1)
	$(PY) scripts/compare_corpus.py --a $$A --b $$B
	@echo
	$(PY) scripts/lab/eval_recuperacion.py --generar $$B --out $${BANCO:-/tmp/preguntas-$$PROFILE.json}
	$(PY) scripts/lab/eval_recuperacion.py --a $$A --b $$B --preguntas $${BANCO:-/tmp/preguntas-$$PROFILE.json}

.PHONY: sync-kb
sync-kb: ## Sube el corpus de un tema a S3 y lanza la ingesta (PROFILE=slug)
	@test -n "$$PROFILE" -o -n "$$CORPUS" || (echo "Uso: make sync-kb PROFILE=autos" && exit 1)
	RAG_PROFILE=$$PROFILE ./scripts/sync-kb.sh $$CORPUS

.PHONY: smoke
smoke: ## Verificación rápida contra el desplegado
	./scripts/smoke.sh

.PHONY: plan
plan: ## terraform plan (no aplica nada; verifica la guarda de cuenta)
	./scripts/deploy.sh plan

.PHONY: deploy
deploy: ## Guarda de cuenta + build + push + apply
	./scripts/deploy.sh

.PHONY: destroy
destroy: ## Destruye la infraestructura (pide confirmación)
	terraform -chdir=infra destroy

# --- UI --------------------------------------------------------------------
# La interfaz web vive en `ui/` y se despliega aparte. Estos objetivos son
# atajos: no se enganchan a `test` ni a `deploy`, y borrar `ui/` no rompe nada.

.PHONY: ui-install
ui-install: ## Instala las dependencias de la interfaz web
	cd ui && npm install

.PHONY: ui-dev
ui-dev: ## Levanta la interfaz web en local (necesita `make run` en otra terminal)
	cd ui && npm run dev

.PHONY: ui-build
ui-build: ## Compila la interfaz web a ui/dist
	cd ui && npm run build

.PHONY: ui-test
ui-test: ## Pruebas de la interfaz web
	cd ui && npm test

.PHONY: docker-build
docker-build: ## Construye la imagen del servicio
	docker build -t $(IMAGE) .

.PHONY: docker-run
docker-run: ## Corre la imagen en local en el puerto 8080
	docker run --rm -p 8080:8080 --env-file .env $(IMAGE)

.PHONY: clean
clean: ## Borra artefactos de pruebas y compilación
	rm -rf .pytest_cache reports build *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
