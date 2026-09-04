locals {
  name = "${var.project}-${var.environment}"

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Repo        = "rag-agent"
  }

  # Puerto del contenedor; coincide con el CMD del Dockerfile.
  container_port = 8080

  metrics_namespace = "rag-agent"

  embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"

  # Los temas se leen de `profiles/*.yaml`: es la misma lista que consume el
  # servicio, así que no hay dos fuentes de verdad que puedan divergir. Añadir
  # un tema es crear su YAML y aplicar — no editar también un tfvars.
  profiles_dir  = "${path.module}/../profiles"
  profile_files = fileset(local.profiles_dir, "*.y*ml")
  profiles = {
    for archivo in local.profile_files :
    yamldecode(file("${local.profiles_dir}/${archivo}")).slug => yamldecode(file("${local.profiles_dir}/${archivo}"))
  }

  # slug → ID de su Knowledge Base. Es lo que la task definition inyecta en el
  # contenedor: el servicio no sabe de Terraform, solo recibe el mapa.
  knowledge_base_ids = { for slug, kb in aws_bedrockagent_knowledge_base.main : slug => kb.id }

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
