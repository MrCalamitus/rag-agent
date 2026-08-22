# Comandos del proyecto. `make help` lista lo disponible.
SHELL := /bin/bash
PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
IMAGE ?= luis-cv/api:local

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

.PHONY: run
run: .venv ## Levanta la API en local (adaptadores locales, sin AWS)
	.venv/bin/uvicorn luis_cv.main:app --reload --port 8080

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
	AWS_PROFILE=$${AWS_PROFILE:-luis} LUISCV_INFERENCE_BACKEND=bedrock $(PY) -m pytest -q tests/rag

.PHONY: test-deployed
test-deployed: .venv ## La misma suite contra el ALB (exige BASE_URL y API_TOKEN)
	@test -n "$$BASE_URL" || (echo "Define BASE_URL=https://... y API_TOKEN=..." && exit 1)
	$(PY) -m pytest -q -m deployed

.PHONY: eval
eval: .venv ## Preguntas de oro y reporte comparativo (GOLDEN=ruta, MODELS=alias)
	$(PY) scripts/eval.py --models $${MODELS:-agente-rag-sonnet} \
		$${GOLDEN:+--golden $$GOLDEN}

.PHONY: corpus
corpus: .venv ## Prepara el corpus (SOURCE=carpeta OUT=destino, fuera del repo)
	@test -n "$$SOURCE" -a -n "$$OUT" || (echo "Uso: make corpus SOURCE=~/docs OUT=~/docs/corpus" && exit 1)
	$(PY) scripts/prep_corpus.py --source $$SOURCE --out $$OUT \
		$${TRANSCRIPCIONES:+--transcripciones $$TRANSCRIPCIONES} $${SKIP:+--skip $$SKIP}

.PHONY: sync-kb
sync-kb: ## Sube el corpus a S3 y lanza la ingesta (CORPUS=ruta)
	./scripts/sync-kb.sh $${CORPUS:-$$LUISCV_CORPUS_DIR}

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
