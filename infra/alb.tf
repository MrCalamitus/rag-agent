resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  # EC2 rechaza cualquier carácter fuera de ASCII en la descripción de un
  # security group, a diferencia de CloudWatch o Bedrock. Por eso estas
  # descripciones van sin acentos y los comentarios sí los llevan.
  description = "Entrada publica al balanceador"
  vpc_id      = aws_vpc.main.id

  lifecycle { create_before_destroy = true }
  tags = { Name = "${local.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = var.certificate_arn != "" ? toset(var.allowed_cidrs) : toset([])

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS publico"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = var.certificate_arn != "" ? "HTTP, solo para redirigir a HTTPS" : "HTTP directo (sin certificado configurado)"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Hacia las tareas"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = local.container_port
  to_port                      = local.container_port
  ip_protocol                  = "tcp"
}

resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [for s in aws_subnet.public : s.id]

  # El presupuesto del contrato §8 permite respuestas de hasta 15 s p95, y una
  # conexión SSE queda abierta mucho más. El default de 60 s cortaría streams
  # legítimos a media respuesta.
  idle_timeout = 120

  drop_invalid_header_fields = true
  enable_deletion_protection = false # entorno desechable

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "api" {
  # name_prefix, no name: permite reemplazar el target group sin destruir el
  # listener que lo referencia.
  name_prefix          = "cv-"
  port                 = local.container_port
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle { create_before_destroy = true }
  tags = { Name = "${local.name}-tg" }
}

resource "aws_lb_listener" "https" {
  count = var.certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # Con certificado, el 80 solo redirige. Sin certificado sirve tráfico en
  # claro: sirve para verificar el despliegue, no para entregar — el token
  # Bearer viajaría legible.
  dynamic "default_action" {
    for_each = var.certificate_arn != "" ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.certificate_arn != "" ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}
