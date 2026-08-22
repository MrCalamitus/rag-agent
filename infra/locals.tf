locals {
  name = "${var.project}-${var.environment}"

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Repo        = "luis-cv"
  }

  # Puerto del contenedor; coincide con el CMD del Dockerfile.
  container_port = 8080

  metrics_namespace = "luis-cv"

  embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"

  knowledge_base_id = aws_bedrockagent_knowledge_base.main.id

  # Los perfiles de inferencia `us.` enrutan a varias regiones; la política
  # necesita el modelo base en cada una.
  foundation_model_arns = flatten([
    for region in var.bedrock_inference_regions : [
      for alias, model_id in var.model_aliases :
      "arn:aws:bedrock:${region}::foundation-model/${replace(model_id, "/^(us|global)\\./", "")}"
    ]
  ])

  inference_profile_arns = [
    for alias, model_id in var.model_aliases :
    "arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:inference-profile/${model_id}"
    if startswith(model_id, "us.") || startswith(model_id, "global.")
  ]

  image = var.container_image != "" ? var.container_image : "${aws_ecr_repository.api.repository_url}:bootstrap"

  # Endpoints de interfaz necesarios para que una tarea sin salida a internet
  # arranque y funcione: imagen (ecr.api + ecr.dkr, con S3 por gateway para las
  # capas), secreto del token, logs, e inferencia y recuperación de Bedrock.
  interface_endpoints = {
    bedrock_runtime       = "bedrock-runtime"
    bedrock_agent_runtime = "bedrock-agent-runtime"
    ecr_api               = "ecr.api"
    ecr_dkr               = "ecr.dkr"
    secretsmanager        = "secretsmanager"
    logs                  = "logs"
  }
}
