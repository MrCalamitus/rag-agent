terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Estado local mientras el proyecto es de una sola persona. Antes de que lo
  # toque un segundo par de manos, mover a S3 con bloqueo:
  #
  # backend "s3" {
  #   bucket       = "luis-cv-tfstate-<account_id>"
  #   key          = "prod/terraform.tfstate"
  #   region       = "us-east-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}
