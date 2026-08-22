# La guarda de cuenta es lo primero que se escribe, no lo último: `apply` debe
# fallar en seco si el perfil apunta a otra cuenta. Un despliegue en la cuenta
# equivocada se limpia a mano, y a veces no del todo.
provider "aws" {
  region              = var.aws_region
  profile             = var.aws_profile
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = local.tags
  }
}
