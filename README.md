# rag-agent

Agente RAG **auditable y reutilizable**: se apunta a una carpeta de documentos,
se declara un tema en un YAML y queda un endpoint compatible con **Open
Responses** desplegado en AWS con arquitectura de grado bancario.

---

## Qué es

`rag-agent` responde preguntas sobre un corpus documental fundamentando **cada
afirmación en el documento que la respalda**. No es un chatbot sobre unos PDFs:
es un agente cuya salida es auditable, porque devuelve junto a la respuesta la
evidencia recuperada y su procedencia.

El problema que resuelve no es "responder bien". Es **responder de forma
verificable**. En un dominio donde una alucinación equivale a afirmar una
credencial falsa —o a publicar una potencia de motor que el fabricante no da—
un agente que suena convincente pero no puede probar lo que dice no sirve. Por
eso la recuperación no es un paso interno oculto: se emite como un ítem de
salida del protocolo, con la consulta ejecutada, los fragmentos recuperados y su
procedencia.

Está construido como implementación conforme del spec **Open Responses
2026-04-24**, de modo que cualquier cliente que hable ese protocolo —SDKs,
gateways, Open WebUI— puede consumirlo sin adaptadores.

### Un servicio, varios temas

Un **tema** es un `profiles/<slug>.yaml`: sobre qué responde el agente, con qué
reglas, cuánta evidencia recupera, qué enmascara y cómo se trocea su corpus.
Cambiar de dominio no es tocar Python.

| Tema | Corpus | Particularidad |
|---|---|---|
| `luis-cv` | Títulos, cédulas y constancias | Enmascara CURP, RFC y teléfonos; postura sustentada ante preguntas de contratación |
| `coches` | 123 fichas técnicas y folletos de 14 marcas | Trocea documentos largos; deduce `marca` de la carpeta; transcribe los PDF de imagen conservando la tabla |

El despliegue sirve **todos los temas a la vez**: se comparte el plano de cómputo
—VPC, ALB, ECS, endpoints— y se duplica solo la Knowledge Base, que sobre S3
Vectors cuesta centavos. Añadir un tema son dos minutos de `apply`, no otros
60 USD/mes. El cliente elige con la cabecera `X-Rag-Profile`.

---

## Empezar

```bash
make install     # entorno y dependencias
make menu        # menú interactivo: configurar, preparar, probar, desplegar
```

El menú recoge los nombres y variables del proyecto —cuenta de AWS, región,
prefijo de recursos, primer tema— y escribe `.env`, `infra/terraform.tfvars` y
`profiles/<tema>.yaml`. Nada de eso está escrito en el código.

```
  Agente RAG  ·  rag-coches  ·  tema: coches  · local/stub
  ────────────────────────────────────────────────────────────────────

  1) Inicializar el proyecto ......... nombres, cuenta AWS y primer tema
  2) Temas ........................... crear, activar o revisar un tema
  3) Preparar corpus ................. documentos → fragmentos indexables
  4) Probar en local ................. conversar con el agente sin AWS
  5) Desplegar ....................... build, push e infraestructura
  6) Sincronizar la base de conocimiento
  7) Evaluar ......................... preguntas de oro y reporte
  8) Estado .......................... qué está hecho y qué falta
  0) Salir
```

Cada opción tiene su equivalente suelto (`make init`, `make corpus PROFILE=…`,
`make deploy`, `make estado`); el menú existe porque reutilizar el agente en un
tema nuevo son cinco pasos encadenados y la mitad de los errores de despliegue
son creer que ya se hizo el anterior.

---

## Arquitectura

```mermaid
flowchart TB
    Cliente["Cliente Open Responses<br/>(SDK / curl / gateway)"]

    subgraph AWS["AWS — cuenta única, región única"]
        ALB["Application Load Balancer<br/>HTTPS · idle_timeout 120s · SSE nativo"]

        subgraph VPC["VPC"]
            subgraph Priv["Subredes privadas"]
                ECS["ECS Fargate<br/>&lt;proyecto&gt;-api<br/>FastAPI + boto3"]
            end
            VPCE["VPC Endpoints (PrivateLink)<br/>bedrock-runtime · bedrock-agent-runtime<br/>s3 · ecr · secrets · logs"]
        end

        subgraph Bedrock["Amazon Bedrock"]
            KB["Knowledge Base<br/>una por tema"]
            LLM["Modelos<br/>Anthropic · GPT"]
            GR["Guardrail<br/>PII · fundamentación"]
        end

        S3["S3<br/>&lt;proyecto&gt;-corpus<br/>un prefijo por tema"]
        OBS["CloudWatch + X-Ray<br/>logs · métricas · trazas"]
        SM["Secrets Manager<br/>token de API"]
    end

    Cliente -->|"POST /v1/responses"| ALB
    ALB --> ECS
    ECS --> VPCE
    VPCE --> KB
    VPCE --> LLM
    VPCE --> GR
    S3 -.->|ingesta| KB
    ECS --> OBS
    ECS --> SM
    ALB -->|"SSE: text/event-stream"| Cliente
```

### Flujo de una petición

1. El ALB termina TLS y enruta al servicio en subredes privadas. Su
   `idle_timeout` está elevado para no cortar respuestas largas.
2. La aplicación valida la petición contra el contrato, resuelve el alias de
   modelo a un ID de Bedrock y el tema (`X-Rag-Profile`) a sus reglas y su
   Knowledge Base.
3. **Recuperación:** `bedrock-agent-runtime:Retrieve` sobre la Knowledge Base
   *de ese tema*. Los fragmentos se emiten como el ítem
   `agente:knowledge_search`.
4. **Inferencia:** `bedrock-runtime:ConverseStream` con el contexto recuperado,
   filtrado por el guardrail.
5. Los tokens se traducen a eventos semánticos de Open Responses y viajan como
   SSE hasta el cliente, sin buffering intermedio.

**Todo el tráfico hacia Bedrock, S3 y ECR viaja por PrivateLink.** Nunca sale a
internet público. Es la razón técnica —no solo declarativa— por la que se
descartó un router de modelos externo.

### Componentes

| Capa | Servicio | Recurso | Por qué |
|---|---|---|---|
| Borde | ALB | `<proyecto>-<entorno>-alb` | Timeouts controlables y SSE nativo. API Gateway impone un límite duro de 29 s que un RAG puede superar |
| Cómputo | ECS Fargate | `<proyecto>-api` | Control total del ciclo de vida de la conexión, sin servidores que administrar |
| Inferencia | Bedrock | Anthropic · GPT | Soberanía de datos: IAM nativo, sin llaves externas ni salida a internet |
| Recuperación | Bedrock KB | `<proyecto>-<tema>` (una por tema) | RAG administrado sin capa de orquestación de terceros |
| Seguridad | Guardrails | `<proyecto>-guardrail` | Filtro de PII y verificación de fundamentación |
| Almacenamiento | S3 | `<proyecto>-corpus-<acct>` | Origen de la ingesta, cifrado y sin acceso público |
| Secretos | Secrets Manager | `<proyecto>/api-token` | Ninguna credencial en imagen ni en variables de entorno |
| Observabilidad | CloudWatch · X-Ray | `<proyecto>-<entorno>` | Trazado por tramos: recuperación e inferencia por separado |

### Stack

`Python` · `FastAPI` · `boto3` · `Docker` · `Terraform`

El código sigue **arquitectura hexagonal**: el núcleo —reglas de veracidad,
enmascarado de identificadores, traducción a eventos— no importa `fastapi` ni
`boto3`; la recuperación y la inferencia entran por puertos. Detalle en
`docs/arquitectura.md`.

Sin LangChain ni LlamaIndex: menos dependencias, menos superficie de ataque,
código auditable y control directo del prompt y del formato de eventos.

---

### PDFs que no se dejan leer

Un corpus real trae documentos ilegibles. La ingesta intenta tres cosas en orden
de coste antes de rendirse, y dice siempre cuál usó:

1. **Leer la capa de texto.** El caso normal.
2. **Descifrar.** Muchos PDF corporativos vienen cifrados con contraseña de
   propietario vacía —restringen copiar e imprimir, no leer— y su texto está
   entero. Es el rescate más barato que existe.
3. **Transcribir.** Solo si de verdad no hay texto, y solo si el perfil lo pide.

```yaml
# profiles/coches.yaml
ocr:
  motor: tablas       # ninguno | tablas | texto
  dpi: 200
  max_paginas: 20
  min_chars_por_pagina: 200
```

`tablas` conserva la rejilla del documento. En una ficha comparativa eso no es un
lujo: una fila de equipamiento leída sin su columna afirma de **todas** las
versiones lo que solo vale para una. `texto` (tesseract) es gratis y offline
pero aplana, y lo avisa.

La interpretación es **opt-in por forma, no por tema**. Una tabla se lee como
ficha comparativa solo si demuestra serlo; una tabla de datos —un balance, un
histórico— se vuelca sin interpretar, con cada fila llevando sus encabezados
dentro para que el troceado no deje cifras huérfanas de su columna. Es lo que
permite apuntar el pipeline a un corpus que nadie ha revisado sin que invente
relaciones entre datos.

Antes de transcribir nada, `make corpus` enseña cuántas páginas va a procesar y
cuánto cuesta; el resultado se cachea por contenido del archivo, así que
reajustar el troceado no vuelve a pagarlo. Una transcripción demasiado pobre para
ser evidencia —un folleto cuyas páginas son mapas— se descarta con su motivo en
vez de indexarse como ruido.

---

## Uso

Todas las peticiones van a `POST /v1/responses` con
`Authorization: Bearer <token>` y `Content-Type: application/json`.

### Elegir el tema

```bash
curl -s "$BASE_URL/v1/profiles" -H "Authorization: Bearer $API_TOKEN"
# {"default":"coches","data":[{"id":"coches",…},{"id":"luis-cv",…}]}

curl -X POST "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Rag-Profile: coches" \
  -d '{"model": "agente-rag-sonnet", "input": "¿Qué motorización tiene la Hilux?"}'
```

Sin la cabecera se usa el tema por defecto, así que un cliente de Open Responses
que no sabe que esto existe sigue funcionando. Un tema desconocido devuelve 400
con `profile_not_found` y la lista de temas válidos.

### Consulta simple

```bash
curl -X POST "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agente-rag-sonnet",
    "input": "¿Qué formación académica tiene y está titulado?"
  }'
```

Respuesta (abreviada). Nótese el **recibo de recuperación** antes del mensaje:

```jsonc
{
  "id": "resp_01H8X...",
  "status": "completed",
  "model": "agente-rag-sonnet",
  "output": [
    {
      "type": "agente:knowledge_search",
      "id": "ks_8f3a1c",
      "status": "completed",
      "queries": ["formación académica", "título profesional"],
      "results": [
        {
          "document_id": "titulo-ingenieria-2019.pdf",
          "chunk": "…",
          "score": 0.91,
          "metadata": { "tipo": "documento_oficial", "anio": 2019 }
        }
      ],
      "latency_ms": 312
    },
    {
      "type": "message",
      "id": "msg_02K9…",
      "role": "assistant",
      "status": "completed",
      "content": [
        {
          "type": "output_text",
          "text": "Cuenta con título de Ingeniería, expedido en 2019 … [titulo-ingenieria-2019.pdf]"
        }
      ]
    }
  ],
  "usage": { "input_tokens": 1840, "output_tokens": 96 }
}
```

### Streaming

```bash
curl -N -X POST "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agente-rag-sonnet",
    "stream": true,
    "input": "Resume su experiencia en la nube."
  }'
```

Secuencia de eventos, conforme al spec:

```
event: response.created
event: response.in_progress
event: response.output_item.added      ← agente:knowledge_search
event: response.output_item.done       ← fragmentos recuperados
event: response.output_item.added      ← message
event: response.content_part.added
event: response.output_text.delta      ← repetido
event: response.output_text.done
event: response.content_part.done
event: response.output_item.done
event: response.completed
data: [DONE]
```

`sequence_number` es monotónico y sin huecos; concatenar los `delta` reproduce
exactamente el texto de `output_text.done`.

### Conversación de varios turnos

El servidor es **sin estado** y no persiste transcripciones. El historial se
envía completo en cada llamada:

```jsonc
{
  "model": "agente-rag-sonnet",
  "input": [
    { "type": "message", "role": "user",
      "content": [{ "type": "input_text", "text": "¿Tiene experiencia en AWS?" }] },
    { "type": "message", "role": "assistant",
      "content": [{ "type": "output_text", "text": "Sí, …" }] },
    { "type": "message", "role": "user",
      "content": [{ "type": "input_text", "text": "¿En qué proyectos?" }] }
  ]
}
```

El cliente es dueño de su contexto y de su retención. Si el cliente no guarda
nada, no existe nada.

### Modelos disponibles

| Alias | Uso |
|---|---|
| `agente-rag-sonnet` | Default. Mejor calidad de razonamiento |
| `agente-rag-haiku` | Menor latencia y costo |
| `agente-rag-gpt` | Contraste entre familias de modelo |

El cliente nunca envía IDs de Bedrock. El mapa de alias se resuelve en el
servidor y se valida al arranque; un alias no reconocido devuelve
`invalid_request` con `code: "model_not_found"`.

### Salud

```bash
curl "$BASE_URL/healthz"   # liveness, sin autenticación
curl "$BASE_URL/readyz"    # verifica Bedrock y KB alcanzables
```

---

## Despliegue

### Requisitos

- AWS CLI con el perfil `luis` configurado
- Terraform ≥ 1.6, Docker, Python ≥ 3.11
- Acceso concedido en Bedrock a los modelos del mapa de alias

### Pasos

```bash
# 1. Verificar identidad y cuenta destino
aws sts get-caller-identity --profile luis

# 2. Configurar (terraform.tfvars está en .gitignore)
cp infra/terraform.tfvars.example infra/terraform.tfvars
# editar: aws_account_id · opcional: certificate_arn para HTTPS

# 3. Infraestructura y aplicación, en un solo paso
./scripts/deploy.sh
```

`deploy.sh` hace el recorrido completo en orden: guarda de cuenta, verificación
de acceso a los modelos, creación del repositorio de imágenes, construcción
`linux/amd64`, publicación, `terraform apply` y **espera a que el rollout de ECS
termine** antes de declarar éxito. Ese último paso importa: Terraform acaba
cuando ECS *acepta* la nueva definición, no cuando la tarea nueva *sirve*.

```bash
# 4. Preparar el corpus de cada tema e ingestarlo en su Knowledge Base
pip install -e ".[ingest]"      # dependencias de la ingesta (no van en la imagen)
make corpus PROFILE=coches      # PDFs → fragmentos + metadatos
make sync-kb PROFILE=coches     # sube a s3://…/coches/ y lanza la ingesta

# 5. Verificar contra el despliegue
export BASE_URL=$(terraform -chdir=infra output -raw base_url)
export API_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id "$(terraform -chdir=infra output -raw api_token_secret_arn)" \
  --query SecretString --output text)

make smoke && make test-deployed
```

`sync-kb.sh` falla si la ingesta indexa cero documentos. Es el modo de falla
silencioso de una base de conocimiento: ingiere nada, el agente responde «no
consta» a todo y parece prudente cuando en realidad está ciego.

`deploy.sh` compara el account ID real contra el configurado y **aborta si no
coinciden**. El provider de Terraform aplica la misma guarda vía
`allowed_account_ids`. Desplegar en la cuenta equivocada no tiene deshacer; se
previene en dos capas.

### Configuración

| Variable | Default | Descripción |
|---|---|---|
| `aws_profile` | `luis` | Perfil de la máquina que despliega |
| `aws_account_id` | — | Requerido. Guarda de cuenta destino |
| `aws_region` | — | Región de despliegue |
| `project` | `rag-agent` | Prefijo y tag de todos los recursos |
| `environment` | `prod` | Sufijo de nombres |
| `default_profile` | *(primero)* | Tema usado sin cabecera `X-Rag-Profile` |

Los temas **no** se declaran aquí: Terraform lee `profiles/*.yaml` y crea una
Knowledge Base por cada uno. Añadir un tema es crear su YAML y aplicar; no hay
una segunda lista que pueda divergir de la primera.

Sobrescribible sin tocar el código:

```bash
terraform apply -var aws_profile=otro -var environment=dev
```

Todo recurso lleva los tags `Project`, `Environment`, `ManagedBy` y `Owner`, lo
que permite aislar el costo del proyecto en Cost Explorer con un filtro.

---

## Estado

**Desplegado y funcionando en AWS.**

| Pieza | Estado |
|---|---|
| Contrato Open Responses (casos A y B) | ✅ 32 casos, verificados en local y contra el ALB |
| Recuperación, veracidad y operación (casos C y D) | ✅ 38 casos, incluidos 25 contra el modelo real |
| Knowledge Base sobre S3 Vectors | ✅ desplegada, 10 de 10 documentos indexados |
| Infraestructura (Terraform) y despliegue | ✅ 72 recursos, ECS Fargate tras ALB |
| Observabilidad | ◐ logs, métricas, panel y alarmas; falta X-Ray y notificación de alarmas |
| Evaluación con preguntas de oro | ◐ 14 preguntas medidas; el plan contempla 20 |
| RAG general reutilizable | ◐ temas por YAML, ingesta de PDF con troceado, menú interactivo y N Knowledge Bases en Terraform; falta desplegar la topología multi-tema |
| **HTTPS en el balanceador** | ⏳ **pendiente** — hoy sirve en claro, el token viaja legible |
| Guardrail administrado de Bedrock | ⏳ pendiente |

**Resultados contra el despliegue**, con recuperación real desde la KB:

| Alias | Modelo | Aciertos | Negativas | TTFT p50 |
|---|---|---|---|---|
| `agente-rag-sonnet` | Claude Sonnet 5 | **14/14** | 5/5 | 2.77 s |
| `agente-rag-haiku` | Claude Haiku 4.5 | 13/14 | 5/5 | **1.61 s** |
| `agente-rag-gpt` | GPT-OSS 120B | 8/14 | 5/5 | 2.15 s |

Cero credenciales inventadas y cero identificadores filtrados en los 42 casos.
Los fallos de GPT son todos de citación: recupera y responde bien, pero no cita
el documento — y aquí la cita no es formato, es el mecanismo de no-repudio.

El servicio también corre **sin AWS**, con recuperación sobre `corpus/` y un
modelo local determinista, que es como corre la suite completa en dos segundos:

```bash
make install        # entorno de desarrollo
make run            # API en http://localhost:8080, sin AWS ni credenciales
```

---

## Pruebas

```bash
make test           # suite completa en local: contrato + RAG + operación
make test-contract  # solo los casos A y B
make test-rag       # solo los casos C
make test-deployed  # la misma aceptación contra el ALB (BASE_URL, API_TOKEN)
make eval           # preguntas de oro, reporte comparativo entre modelos
make smoke          # verificación rápida contra el desplegado
```

La suite verifica el contrato (esquema, errores, orden y numeración de
eventos), la recuperación (cita el documento correcto) y la **veracidad**: ante
una credencial inexistente el agente debe negar, no inventar. Ese caso tiene
tolerancia cero.

Dos pruebas gobiernan la entrega:

- **Sin buffering:** los deltas deben llegar espaciados en el tiempo a través
  del ALB. Si llegan todos al final, el streaming no existe en la práctica.
- **Cero credenciales inventadas:** una sola invalida el despliegue.

---

## Seguridad y privacidad

El corpus contiene documentos de identidad y credenciales profesionales. El
diseño lo trata en consecuencia:

- **Retención cero.** `store: true` se rechaza explícitamente. No se persisten
  entradas ni salidas.
- **Logs sin PII.** Solo identificadores, contadores y latencias. Nunca el
  texto del turno.
- **Enmascarado en la salida.** El agente confirma la existencia y vigencia de
  una credencial; los identificadores completos (cédula, CURP, RFC, teléfono)
  van enmascarados salvo petición explícita y autenticada. El enmascarado
  también se aplica sobre el stream, sin partir un identificador entre dos
  fragmentos de texto.
- **Sin salida a internet.** PrivateLink para todo servicio de AWS consumido.
- **Permisos mínimos.** El rol de tarea concede solo `InvokeModelWithResponseStream`,
  `Retrieve` sobre el ARN específico de la KB y `ApplyGuardrail`. Sin comodines.
- **Errores opacos.** Ningún ARN, ID de cuenta ni traza de excepción llega al
  cliente; se devuelve un `request_id` para correlación.
- El directorio `corpus/` está excluido de control de versiones.

> **Pendiente, y es la brecha abierta más relevante:** sin `certificate_arn`
> configurado el balanceador escucha en HTTP y el token *Bearer* viaja en
> claro. Sirve para verificar el despliegue; **no para exponer el endpoint**.
> El listener HTTPS está escrito y se activa poniendo el ARN de un certificado
> de ACM en `terraform.tfvars`.

---

## Limitaciones conocidas

Declaradas de forma deliberada, no omitidas:

| No implementado | Razón |
|---|---|
| Transporte WebSocket | Opcional en el spec; no aporta al caso de uso |
| `previous_response_id` | Exigiría persistir transcripciones, en conflicto con la retención cero |
| `GET /v1/responses/{id}` | Consecuencia de no almacenar respuestas |
| Herramientas de función externas | Agente de dominio cerrado; no cede control al cliente |
| Entrada multimodal | El corpus es texto; se rechaza con error explícito |

Un endpoint que finge soportar todo y falla en silencio es peor que uno con una
superficie honesta y documentada.

---

## Costos

El componente dominante **no es la inferencia ni el vector store**: son los
**VPC endpoints**, unos 105 USD al mes. Con el ALB y una tarea de Fargate, el
costo fijo ronda los 130 USD mensuales.

Que el almacén vectorial no aparezca en esa lista es el resultado de la
decisión más rentable del proyecto: OpenSearch Serverless factura unidades de
cómputo de forma continua —del orden de 350 USD al mes de piso— mientras que
S3 Vectors cobra por almacenamiento y consulta, y con diez documentos eso son
céntimos. La comparación con números está en la bitácora §12.

Los endpoints son caros a propósito: sin ellos el tráfico a Bedrock saldría por
internet y el argumento de soberanía del dato se caería solo. Si el presupuesto
apretara, la palanca correcta es desplegarlos en una sola zona de
disponibilidad —la mitad del gasto— no volver a un NAT Gateway.

Para desmontar todo:

```bash
make destroy
```

---

## Documentación

Cada documento responde una pregunta distinta:

| Documento | Responde |
|---|---|
| `docs/contrato-open-responses.md` | **Qué es correcto** — contrato normativo y suite de aceptación |
| `docs/PLAN.md` | **Cómo se construyó** — etapas, criterios de salida y decisiones abiertas |
| `docs/Bitacora.MD` | **Por qué se decidió así** — hipótesis evaluadas y rutas descartadas |
| `docs/arquitectura.md` | **Cómo está hecho** — capas, puertos y adaptadores |

La bitácora documenta también lo que **no** se eligió y por qué: OpenRouter
frente a Bedrock, Lambda y API Gateway frente a Fargate, y frameworks de
orquestación frente al SDK nativo.