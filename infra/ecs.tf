resource "aws_security_group" "tasks" {
  name_prefix = "${local.name}-tasks-"
  description = "Tareas de la API en subredes privadas"
  vpc_id      = aws_vpc.main.id

  lifecycle { create_before_destroy = true }
  tags = { Name = "${local.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Desde el ALB"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = local.container_port
  to_port                      = local.container_port
  ip_protocol                  = "tcp"
}

# Única salida permitida: HTTPS hacia los endpoints de VPC. No hay ruta a
# internet, así que esta regla describe todo lo que la tarea puede alcanzar.
resource "aws_vpc_security_group_egress_rule" "tasks_to_endpoints" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "HTTPS hacia los endpoints de VPC"
  referenced_security_group_id = aws_security_group.endpoints.id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}

# S3 por gateway endpoint no pasa por un security group referenciable: se
# permite por prefix list, que solo cubre los rangos de S3 en la región.
data "aws_prefix_list" "s3" {
  name = "com.amazonaws.${var.aws_region}.s3"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_s3" {
  security_group_id = aws_security_group.tasks.id
  description       = "HTTPS hacia S3 (capas de imagen de ECR)"
  prefix_list_id    = data.aws_prefix_list.s3.id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# --- IAM ---------------------------------------------------------------------

data "aws_iam_policy_document" "assume_ecs_tasks" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Rol de ejecución: lo usa el agente de ECS para traer la imagen, leer el
# secreto y escribir logs. No es el rol con el que corre la aplicación.
resource "aws_iam_role" "execution" {
  name_prefix        = "${var.project}-exec-"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
  tags               = { Name = "${local.name}-execution" }
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "LeerElTokenDeApi"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.api_token.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name_prefix = "secrets-"
  role        = aws_iam_role.execution.id
  policy      = data.aws_iam_policy_document.execution_secrets.json
}

# Rol de tarea: los permisos de la aplicación. Sin comodines, y sin `bedrock:*`.
data "aws_iam_policy_document" "task" {
  statement {
    sid = "InvocarModelosConStreaming"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Un perfil de inferencia enruta a varias regiones: hacen falta el ARN del
    # perfil y el del modelo base en cada región destino. Si falta una, la
    # invocación falla de forma intermitente y solo bajo carga.
    resources = concat(local.inference_profile_arns, local.foundation_model_arns)
  }

  statement {
    sid       = "RecuperarDeLaKnowledgeBase"
    actions   = ["bedrock:Retrieve"]
    resources = [for kb in aws_bedrockagent_knowledge_base.main : kb.arn]
  }

  dynamic "statement" {
    for_each = var.guardrail_id != "" ? [1] : []
    content {
      sid       = "AplicarGuardrail"
      actions   = ["bedrock:ApplyGuardrail"]
      resources = ["arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:guardrail/${var.guardrail_id}"]
    }
  }

  # Solo el prefijo de originales, nunca el corpus indexado. Son dos cosas
  # distintas y por eso viven en prefijos distintos: lo que el agente puede
  # recitar y lo que un lector puede abrir. Dar acceso al bucket entero borraría
  # esa frontera en el único sitio donde de verdad se aplica.
  statement {
    sid       = "LeerDocumentosOriginales"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.corpus.arn}/originales/*"]
  }

  statement {
    sid       = "PublicarMetricasPropias"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData no admite ARN de recurso
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.metrics_namespace]
    }
  }
}

resource "aws_iam_role" "task" {
  name_prefix        = "${var.project}-task-"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
  tags               = { Name = "${local.name}-task" }
}

resource "aws_iam_role_policy" "task" {
  name_prefix = "app-"
  role        = aws_iam_role.task.id
  policy      = data.aws_iam_policy_document.task.json
}

# --- Cluster, tarea y servicio -----------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Name = "${local.name}-cluster" }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.image
    essential = true

    portMappings = [{
      containerPort = local.container_port
      protocol      = "tcp"
    }]

    environment = [
      { name = "RAG_ENVIRONMENT", value = var.environment },
      { name = "RAG_LOG_LEVEL", value = "INFO" },
      { name = "RAG_AWS_REGION", value = var.aws_region },
      { name = "RAG_INFERENCE_BACKEND", value = "bedrock" },
      { name = "RAG_RETRIEVAL_BACKEND", value = "bedrock" },
      # Mapa slug → KB. El servicio sirve todos los temas a la vez y el cliente
      # elige el suyo con la cabecera `X-Rag-Profile`.
      { name = "RAG_PROFILE_KNOWLEDGE_BASES", value = jsonencode(local.knowledge_base_ids) },
      { name = "RAG_DEFAULT_PROFILE", value = var.default_profile },
      { name = "RAG_GUARDRAIL_ID", value = var.guardrail_id },
      { name = "RAG_DOCUMENTS_BUCKET", value = aws_s3_bucket.corpus.id },
      { name = "RAG_MODEL_ALIASES", value = jsonencode(var.model_aliases) },
      { name = "RAG_MODEL_SAMPLING", value = jsonencode(var.model_sampling) },
    ]

    secrets = [
      { name = "RAG_API_TOKEN", valueFrom = aws_secretsmanager_secret.api_token.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${local.container_port}/healthz', timeout=2).status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 10
    }
  }])

  tags = { Name = "${local.name}-task-def" }
}

resource "aws_ecs_service" "api" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [for s in aws_subnet.private : s.id]
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = local.container_port
  }

  health_check_grace_period_seconds = 30

  # Un despliegue que no pasa el health check vuelve solo a la versión anterior
  # en vez de dejar el servicio caído esperando a que alguien mire.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]

  tags = { Name = "${local.name}-service" }
}
