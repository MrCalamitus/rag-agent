output "base_url" {
  description = "URL del endpoint. Va directo a BASE_URL para el smoke y la suite desplegada."
  value       = var.certificate_arn != "" ? "https://${aws_lb.main.dns_name}" : "http://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  description = "Repositorio de imágenes al que empuja deploy.sh."
  value       = aws_ecr_repository.api.repository_url
}

output "api_token_secret_arn" {
  description = "Secreto con el token Bearer. Leerlo con `aws secretsmanager get-secret-value`."
  value       = aws_secretsmanager_secret.api_token.arn
}

output "cluster_name" {
  value       = aws_ecs_cluster.main.name
  description = "Cluster de ECS, para forzar un despliegue nuevo."
}

output "service_name" {
  value       = aws_ecs_service.api.name
  description = "Servicio de ECS."
}

output "dashboard_url" {
  description = "Panel de CloudWatch con TTFT, recuperación y errores."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "log_group" {
  value       = aws_cloudwatch_log_group.api.name
  description = "Grupo de logs de la aplicación."
}

output "knowledge_base_ids" {
  description = "slug del tema → ID de su Knowledge Base. Lo consume sync-kb.sh."
  value       = local.knowledge_base_ids
}

output "data_source_ids" {
  description = "slug del tema → ID de su origen de datos."
  value       = { for slug, ds in aws_bedrockagent_data_source.corpus : slug => ds.data_source_id }
}

output "profiles" {
  description = "Temas que sirve este despliegue, leídos de profiles/*.yaml."
  value       = sort(keys(local.profiles))
}

output "corpus_bucket" {
  description = "Bucket al que se sincroniza el corpus preparado."
  value       = aws_s3_bucket.corpus.id
}
