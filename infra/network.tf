# Red mínima y cerrada: el ALB en subredes públicas, las tareas en privadas, y
# **sin NAT Gateway**. Todo el tráfico hacia AWS sale por PrivateLink.
#
# Esto no es una optimización de costo, aunque también lo sea: la bitácora §2
# descartó un router de modelos externo por no exponer datos a internet
# público. Con NAT, la llamada de Fargate a Bedrock saldría a internet y ese
# argumento se caería solo. Con endpoints, el tráfico nunca abandona la red de
# AWS y la tesis queda respaldada por la topología, no por la narrativa.
#
# Costo: cada endpoint de interfaz ronda 0.01 USD/hora por AZ. Son seis
# endpoints × 2 AZ ≈ 105 USD/mes, y es la partida más cara de esta
# infraestructura. Un NAT Gateway saldría más barato; se elige lo contrario a
# propósito.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # obligatorio para que resuelvan los endpoints

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  for_each = { for i, az in var.azs : az => i }

  vpc_id                  = aws_vpc.main.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, each.value)
  map_public_ip_on_launch = false # solo vive aquí el ALB, que trae su propia IP

  tags = { Name = "${local.name}-public-${each.key}", Tier = "public" }
}

resource "aws_subnet" "private" {
  for_each = { for i, az in var.azs : az => i }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value + 10)

  tags = { Name = "${local.name}-private-${each.key}", Tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-rt-public" }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# Sin ruta a 0.0.0.0/0: desde aquí solo se llega a AWS por PrivateLink.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-rt-private" }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# --- Endpoints ---------------------------------------------------------------

resource "aws_security_group" "endpoints" {
  name_prefix = "${local.name}-vpce-"
  description = "Entrada HTTPS a los endpoints de VPC desde las tareas"
  vpc_id      = aws_vpc.main.id

  lifecycle { create_before_destroy = true }
  tags = { Name = "${local.name}-vpce" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_tasks" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS desde las tareas de ECS"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}

# S3 va por gateway endpoint: gratis, y es por donde viajan las capas de las
# imágenes de ECR.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-vpce-s3" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for s in aws_subnet.private : s.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-vpce-${each.key}" }
}
