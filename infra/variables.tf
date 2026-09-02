variable "aws_account_id" {
  description = "Cuenta donde se despliega. Sin default: obliga a declararla y habilita la guarda del provider."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "El account ID son 12 dígitos."
  }
}

variable "aws_profile" {
  description = "Perfil de credenciales locales."
  type        = string
  default     = "luis"
}

variable "aws_region" {
  description = "Región del despliegue."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Prefijo y tag de toda la infraestructura."
  type        = string
  default     = "rag-agent"
}

variable "environment" {
  description = "Entorno lógico."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "Rango de la VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "azs" {
  description = "Zonas de disponibilidad. Dos bastan para el ALB y evitan pagar de más."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "certificate_arn" {
  description = <<-EOT
    ARN del certificado ACM para el listener HTTPS. Si se deja vacío el ALB
    escucha en HTTP:80, que sirve para probar el despliegue pero NO para
    entregar: el token de API viajaría en claro.
  EOT
  type        = string
  default     = ""
}

variable "allowed_cidrs" {
  description = "Quién puede llegar al ALB. Restringir antes de exponer de verdad."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "container_image" {
  description = "Imagen a desplegar. Vacío = el tag :bootstrap del ECR del propio proyecto."
  type        = string
  default     = ""
}

variable "task_cpu" {
  description = "Unidades de CPU de la tarea Fargate."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Memoria de la tarea Fargate en MiB."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Tareas en ejecución."
  type        = number
  default     = 1
}

variable "model_aliases" {
  description = "Mapa alias público → ID de Bedrock. El cliente nunca envía IDs (contrato §3)."
  type        = map(string)
  default = {
    "agente-rag-sonnet" = "us.anthropic.claude-sonnet-5"
    "agente-rag-haiku"  = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    "agente-rag-gpt"    = "openai.gpt-oss-120b-1:0"
  }
}

variable "model_sampling" {
  description = "Qué alias acepta `temperature`. Sonnet 5 la dejó deprecada."
  type        = map(bool)
  default = {
    "agente-rag-sonnet" = false
    "agente-rag-haiku"  = true
    "agente-rag-gpt"    = true
  }
}

variable "bedrock_inference_regions" {
  description = <<-EOT
    Regiones a las que puede enrutar un perfil de inferencia `us.`. La política
    IAM debe permitir el modelo base en todas ellas, no solo en la región del
    despliegue: si falta una, la invocación falla de forma intermitente y solo
    bajo carga, que es el peor modo de fallo posible.
  EOT
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}

variable "embedding_model_id" {
  description = "Modelo de embeddings de la Knowledge Base. Titan v2 es multilingüe, que importa: el corpus está en español y las preguntas pueden llegar en inglés."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dimension" {
  description = "Dimensión del índice vectorial. Debe coincidir con la salida del modelo de embeddings (Titan v2: 1024)."
  type        = number
  default     = 1024
}

variable "default_profile" {
  description = <<-EOT
    Tema que se usa cuando la petición no manda la cabecera `X-Rag-Profile`.
    Vacío = el primero por orden alfabético de archivo en profiles/.
  EOT
  type        = string
  default     = ""
}

variable "guardrail_id" {
  description = "ID del guardrail de Bedrock. Vacío = sin guardrail administrado."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "Retención de logs. Corta a propósito: los logs no llevan PII, pero tampoco hay razón para guardarlos un año."
  type        = number
  default     = 30
}

variable "ttft_p95_alarm_seconds" {
  description = "Umbral de alarma para el p95 de TimeToFirstToken (contrato §8)."
  type        = number
  default     = 5
}
