# Contrato del Endpoint — Agente RAG compatible con Open Responses

> Documento de diseño normativo. Define qué se implementa, qué **no**, y cómo se
> verifica. Se escribe **antes** del código: es la fuente de verdad contra la que
> corren las pruebas de aceptación.
>
> Base normativa: **Open Responses, versión 2026-04-24** (openresponses.org).
> Slug de implementador: `agente` (configurable; debe ser único y estable).
> Los términos MUST / SHOULD / MAY se interpretan según BCP 14 (RFC 2119).

---

## 0. Alcance y postura de cumplimiento

La API es un **superset conforme** de Open Responses: implementa el núcleo del
spec y añade una herramienta hospedada propia para recuperación documental,
usando el mecanismo de extensión que el propio spec define.

**Se implementa (núcleo):**

- `POST /v1/responses` — creación de respuesta, con y sin streaming.
- Transporte SSE con eventos semánticos y ciclo de vida completo de ítems.
- Ítems `message` (entrada y salida) con contenido `input_text` / `output_text`.
- Herramienta hospedada `agente:knowledge_search` (RAG sobre Bedrock KB).
- Objeto de error estructurado, en respuesta HTTP y como evento de stream.
- `store: false` por defecto, sin persistencia de transcripciones.

**No se implementa (declarado, no omitido):**

| Fuera de alcance | Razón |
|---|---|
| Transporte WebSocket | Añadido en 2026-04-24; es MAY, no MUST. No aporta al caso de uso |
| `previous_response_id` | Requiere estado persistido; ver §7, decisión deliberada |
| `GET /v1/responses/{id}` | Consecuencia de `store: false` |
| Herramientas de función externas (`function_call`) | El agente es de dominio cerrado; no cede control al cliente |
| Ítems `reasoning` | No se expone traza interna al cliente |
| Entrada multimodal (imagen, audio) | El corpus es texto; se rechaza con `invalid_request` |
| `/responses/compact` | Depende de continuación conversacional |

Declarar los no-objetivos **es parte del entregable**. Un endpoint que finge
soportar todo y falla en silencio es peor que uno con superficie honesta.

---

## 1. Superficie HTTP

```
POST /v1/responses          # crear respuesta
GET  /healthz               # liveness — sin auth, para el health check del ALB
GET  /readyz                # readiness — verifica Bedrock y KB alcanzables
```

### Cabeceras de petición

| Cabecera | Obligatoria | Valor |
|---|---|---|
| `Authorization` | Sí | `Bearer <token>` |
| `Content-Type` | Sí | `application/json` |

El cuerpo MUST ir codificado como `application/json`. Ausencia o valor
distinto → `invalid_request` (400).

### Cabeceras de respuesta

| Modo | `Content-Type` |
|---|---|
| No streaming | `application/json` |
| Streaming | `text/event-stream` |

En streaming se añade `Cache-Control: no-cache` y `X-Accel-Buffering: no`.
El segundo es defensivo contra proxies que acumulan la respuesta; sin él, el
SSE llega de golpe al final y el streaming deja de existir en la práctica.

---

## 2. Esquema de petición

```jsonc
{
  "model": "agente-rag-sonnet",       // requerido — alias, ver §3
  "input": [                           // requerido — string o array de ítems
    {
      "type": "message",
      "role": "user",
      "content": [
        { "type": "input_text", "text": "¿Cuál es su cédula profesional?" }
      ]
    }
  ],
  "instructions": "…",                 // opcional — se antepone al system prompt
  "stream": false,                     // opcional — default false
  "store": false,                      // opcional — solo se acepta false
  "max_output_tokens": 2048,           // opcional
  "temperature": 0.2,                  // opcional
  "tool_choice": "auto",               // opcional — auto | none | required
  "truncation": "disabled",            // opcional — auto | disabled
  "metadata": { "caso": "demo-01" }    // opcional — se propaga a los logs
}
```

**Reglas de validación**

1. `input` acepta un string simple como azúcar sintáctica; internamente se
   normaliza a un ítem `message` con `role: "user"`.
2. `role` admite `system`, `developer`, `user`, `assistant`. `system` y
   `developer` se anexan al prompt de sistema, no al turno actual.
3. `store: true` → `invalid_request`, `param: "store"`. Se rechaza de forma
   explícita en lugar de aceptarlo y no persistir nada: fallar ruidosamente es
   preferible a mentir sobre la retención de datos.
4. `truncation: "disabled"` es el default. Si el contexto excede la ventana, la
   petición falla en vez de descartar contenido en silencio — en un agente
   sobre documentos oficiales, perder un fragmento sin avisar es inaceptable.
5. Campos desconocidos se ignoran (tolerancia hacia adelante), pero se registran
   en el log a nivel `WARN` con su nombre.

---

## 3. Mapa de modelos

El cliente nunca envía un ID de Bedrock. Envía un alias estable; el servidor
resuelve. Esto permite cambiar de modelo sin romper clientes, y es el punto
donde el valor "multi-proveedor" de Open Responses se hace concreto.

| Alias (`model`) | Backend | Uso |
|---|---|---|
| `agente-rag-sonnet` | Claude Sonnet en Bedrock | Default. Respuestas de calidad |
| `agente-rag-haiku` | Claude Haiku en Bedrock | Baja latencia / costo |
| `agente-rag-gpt` | Modelo GPT en Bedrock | Contraste multi-proveedor |

Un alias no reconocido → `invalid_request`, `code: "model_not_found"`,
`param: "model"`.

Los IDs concretos de Bedrock viven en configuración (variables de entorno o
Parameter Store), **nunca** en el código. El mapa se resuelve al arranque y se
valida contra los modelos con acceso concedido; si un alias configurado no
está disponible, el servicio no pasa `readyz`.

---

## 4. Streaming — secuencia canónica de eventos

Reglas normativas que se verifican en pruebas:

- El campo `event:` de SSE MUST coincidir con el `type` del cuerpo.
- No se usa el campo `id:` de SSE.
- `sequence_number` es monotónicamente creciente, sin huecos, empezando en 0.
- El evento terminal MUST ser la cadena literal `[DONE]`.

### Camino feliz con recuperación

```
event: response.created            → status: "in_progress"
event: response.in_progress

# — herramienta hospedada de recuperación —
event: response.output_item.added  → item: agente:knowledge_search, in_progress
event: response.output_item.done   → item: agente:knowledge_search, completed
                                      (incluye query y chunks recuperados)

# — mensaje del asistente —
event: response.output_item.added  → item: message, role assistant, in_progress
event: response.content_part.added → part: output_text, text ""
event: response.output_text.delta  → delta: "Según"      ┐
event: response.output_text.delta  → delta: " el"        ├ N veces
event: response.output_text.delta  → delta: " documento" ┘
event: response.output_text.done   → text completo
event: response.content_part.done  → part completo
event: response.output_item.done   → item completo, status completed

event: response.completed          → objeto Response final con usage
data: [DONE]
```

El orden `output_item.added` → `content_part.added` → deltas →
`<content>.done` → `content_part.done` → `output_item.done` es **normativo**.
Un cliente conforme reconstruye la respuesta completa solo con los eventos.

### El ítem de recuperación (extensión propia)

Aquí está el núcleo del argumento de auditabilidad. En vez de que el RAG sea
un paso invisible dentro del servidor, se emite como un **ítem de salida** — un
recibo verificable de qué se recuperó y con qué consulta:

```jsonc
{
  "type": "agente:knowledge_search",
  "id": "ks_8f3a1c...",
  "status": "completed",
  "queries": ["cédula profesional", "número de cédula"],
  "results": [
    {
      "document_id": "s3://…/cedula-profesional.pdf",
      "chunk": "…",
      "score": 0.87,
      "metadata": { "tipo": "documento_oficial", "anio": 2021 }
    }
  ],
  "latency_ms": 340
}
```

Todo ítem MUST llevar `id`, `type` y `status`. Los tipos de extensión MUST
llevar el prefijo del slug del implementador.

Consecuencia práctica: **cualquier respuesta del agente es auditable a
posteriori sin abrir un log**. Quien consume el endpoint ve la evidencia que
sustentó la respuesta en la misma carga útil. Esto convierte la observabilidad
de la §5 de la bitácora en algo que el cliente puede verificar, no solo el
operador.

---

## 5. Errores

| `type` | HTTP | Cuándo |
|---|---|---|
| `invalid_request` | 400 | Esquema inválido, alias desconocido, `store: true` |
| `not_found` | 404 | Ruta o recurso inexistente |
| `too_many_requests` | 429 | Límite de tasa excedido |
| `server_error` | 500 | Fallo interno no atribuible al cliente |
| `model_error` | 500 | Bedrock falla con una petición por lo demás válida |

Forma del objeto:

```jsonc
{
  "error": {
    "message": "El modelo solicitado 'gpt-5' no existe.",
    "type": "invalid_request",
    "param": "model",
    "code": "model_not_found"
  }
}
```

**Errores a mitad de stream.** Cuando ya se envió `200 OK` y cabeceras, no se
puede cambiar el código de estado. El error se emite como evento y MUST ir
seguido de `response.failed`, y luego `[DONE]`:

```
event: error
event: response.failed
data: [DONE]
```

Un cliente que ve `[DONE]` sin `response.completed` sabe que la respuesta está
incompleta. Cerrar el socket a secas deja al cliente sin poder distinguir un
fallo de una respuesta corta.

Códigos de error internos que nunca se propagan al cliente: mensajes de
excepción de boto3, ARNs, IDs de cuenta, rutas de S3. Se registran con el
`request_id` y el cliente recibe ese `request_id` para correlación.

---

## 6. Modelo de datos y privacidad

El corpus son documentos de identidad y credenciales profesionales: CV, título,
cédula profesional, constancias de cursos. Esto tiene tres consecuencias de
diseño que no aplicarían a un corpus corporativo genérico.

**1. Retención cero por defecto.** `store: false` no es una preferencia, es una
postura. No se persisten transcripciones, ni entradas ni salidas. Los logs
llevan hashes y contadores, nunca el texto del turno.

**2. Guardrail de salida contra fuga de identificadores.** Un número de cédula,
CURP o RFC puede estar en el corpus por necesidad (el agente debe poder
confirmar que existe un título registrado) pero no debe emitirse íntegro ante
una pregunta abierta. Política: el agente confirma la existencia y la vigencia
de una credencial; los identificadores completos se enmascaran salvo petición
explícita y autenticada.

**3. La alucinación aquí es un problema de veracidad, no de calidad.** Que el
agente invente una certificación es afirmar una credencial falsa. Por eso:

- El prompt de sistema obliga a responder **solo** desde los fragmentos
  recuperados.
- Si no hay evidencia suficiente, la respuesta correcta es *"eso no consta en
  los documentos disponibles"* — y esa respuesta cuenta como **acierto** en la
  evaluación, no como fallo.
- Toda afirmación sobre una credencial va acompañada del `document_id` que la
  sustenta.

Esta última regla es la que hace del `agente:knowledge_search` algo más que
adorno: es el mecanismo de no-repudio de la respuesta.

---

## 7. Decisión: sin `previous_response_id`

El spec define `previous_response_id` como continuación: el servidor carga la
entrada y la salida de la respuesta previa y las concatena en orden
(`previous.input` → `previous.output` → `input` nuevo).

Implementarlo exige persistir transcripciones completas, lo que contradice
directamente la postura de retención cero de la §6. Se elige la postura de
datos sobre la comodidad conversacional.

**Alternativa ofrecida al cliente:** el historial se envía completo en `input`
en cada llamada. El servidor permanece sin estado; el cliente es dueño de su
propio contexto y de su retención. Si el cliente no guarda nada, no existe
nada. Si en el futuro se requiere continuación, la ruta es memoria local por
conexión (WebSocket, §WebSocket Continuation del spec), que permite continuar
con `store: false` sin escribir a disco.

Documentar esto es más valioso que implementarlo a medias.

---

## 8. Presupuesto de latencia

| Tramo | Objetivo | Límite duro |
|---|---|---|
| Validación + normalización | < 10 ms | 50 ms |
| Recuperación (Bedrock KB) | < 500 ms | 2 s |
| Primer token (TTFT, extremo a extremo) | < 2 s | 5 s |
| Respuesta completa (p95) | < 15 s | — |
| Idle timeout del ALB | — | 120 s |

El TTFT es la métrica que decide si la demo se siente viva. Se mide y se
publica como métrica propia en CloudWatch desde el primer despliegue, no al
final.

---

## 9. Casos de prueba de aceptación

Cada caso es ejecutable y tiene aserción binaria. Constituyen la suite de
regresión: se corren en local y contra el despliegue.

### A. Contrato — no streaming

| # | Caso | Aserción |
|---|---|---|
| A1 | Petición mínima válida | 200; `Content-Type: application/json`; `output[]` con un `message`; cada ítem tiene `id`, `type`, `status` |
| A2 | `input` como string simple | 200; equivalente a A1 |
| A3 | Falta `Authorization` | 401; cuerpo de error bien formado |
| A4 | `Content-Type: text/plain` | 400; `type: "invalid_request"` |
| A5 | JSON malformado | 400; no expone traza de excepción |
| A6 | Falta `model` | 400; `param: "model"` |
| A7 | Alias inexistente | 400; `code: "model_not_found"` |
| A8 | `store: true` | 400; `param: "store"` |
| A9 | Campo desconocido presente | 200; se ignora; aparece `WARN` en el log |
| A10 | Ruta inexistente | 404; `type: "not_found"` |

### B. Contrato — streaming

| # | Caso | Aserción |
|---|---|---|
| B1 | `stream: true` | `Content-Type: text/event-stream` |
| B2 | Orden de eventos | Coincide con la secuencia canónica de §4 |
| B3 | `event:` vs `type` | Idénticos en todos los eventos |
| B4 | `sequence_number` | Monotónico, sin huecos, arranca en 0 |
| B5 | Evento terminal | La última línea es literalmente `data: [DONE]` |
| B6 | Reconstrucción | Concatenar los `delta` == `text` de `output_text.done` |
| B7 | TTFT | Primer `delta` en < 2 s |
| B8 | Sin buffering | Los deltas llegan espaciados, no todos en el último ms |
| B9 | Fallo a media respuesta | `error` → `response.failed` → `[DONE]`; nunca `response.completed` |
| B10 | Cliente corta la conexión | El servidor cancela la inferencia; sin task colgada |

**B8 es la prueba que salva el despliegue.** Se ejecuta contra el ALB, no solo
en local: es exactamente donde el streaming se rompe en silencio.

### C. RAG y veracidad

| # | Caso | Aserción |
|---|---|---|
| C1 | Pregunta con respuesta en el corpus | Respuesta correcta + ítem `knowledge_search` con el `document_id` correcto |
| C2 | Pregunta fuera del corpus | Declina explícitamente; **no** inventa |
| C3 | Pregunta capciosa sobre credencial inexistente | Niega. Cero tolerancia a falsos positivos |
| C4 | Pregunta ambigua entre dos documentos | Recupera ambos; distingue en la respuesta |
| C5 | Pregunta en inglés sobre corpus en español | Responde correctamente pese al idioma cruzado |
| C6 | Solicitud de identificador completo | Enmascarado conforme a §6.2 |
| C7 | Intento de inyección de prompt en el turno | Ignora la instrucción; se mantiene en el dominio |
| C8 | `tool_choice: "none"` | No recupera; responde desde el modelo o declina |

### D. Operación

| # | Caso | Aserción |
|---|---|---|
| D1 | `/healthz` sin auth | 200 |
| D2 | `/readyz` con Bedrock inalcanzable | 503 |
| D3 | Traza en X-Ray | Subsegmentos separados de recuperación e inferencia |
| D4 | Correlación de logs | El `request_id` del error aparece en CloudWatch |
| D5 | Log sin PII | Ninguna entrada contiene texto del turno ni identificadores |
| D6 | Concurrencia | 10 streams simultáneos sin degradar el TTFT |
| D7 | Límite de tasa | Petición 21 en un minuto → 429, `type: "too_many_requests"` |

---

## 10. Verificación rápida

```bash
# Camino feliz, no streaming
curl -sS -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"agente-rag-sonnet","input":"¿Qué formación académica tiene?"}' \
  | jq '.output[] | {type, status}'

# Streaming, con marcas de tiempo — verifica B7 y B8 de un golpe
curl -sSN -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"agente-rag-sonnet","stream":true,"input":"Resume su experiencia en la nube."}' \
  | while IFS= read -r line; do printf '%s %s\n' "$(date +%s.%N)" "$line"; done

# Caso negativo — debe declinar, no inventar
curl -sS -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"agente-rag-sonnet","input":"¿Tiene certificación CISSP?"}'
```

---

## Definición de terminado

El endpoint se considera entregable cuando:

1. Los 35 casos de §9 pasan **contra el despliegue en AWS**, no solo en local.
2. La suite corre con un comando y emite un reporte.
3. B8 (sin buffering) y C3 (cero credenciales inventadas) pasan. Son los dos
   que, si fallan, invalidan la entrega completa: uno rompe el producto, el
   otro rompe la confianza.
