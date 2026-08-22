# El token de API nunca vive en la imagen ni en variables de entorno del plan:
# se inyecta como secreto de la task definition y ECS lo resuelve al arrancar.
resource "random_password" "api_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "api_token" {
  name_prefix             = "${var.project}/api-token-"
  description             = "Token Bearer del endpoint /v1/responses"
  recovery_window_in_days = 0 # entorno desechable; subir a 7 antes de entregar

  tags = { Name = "${local.name}-api-token" }
}

resource "aws_secretsmanager_secret_version" "api_token" {
  secret_id     = aws_secretsmanager_secret.api_token.id
  secret_string = random_password.api_token.result

  # El valor puede rotarse fuera de Terraform sin que un `apply` lo pise.
  lifecycle {
    ignore_changes = [secret_string]
  }
}
