# La aplicación ya emite logs JSON con `ttft_ms`, `chunks_retrieved` y
# `grounded`, sin texto del turno ni PII. Aquí esos campos se convierten en
# métricas: no hace falta tocar el código para tener la alarma del §8.

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${local.name}-logs" }
}

resource "aws_cloudwatch_log_metric_filter" "ttft" {
  name           = "${local.name}-ttft"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"response.completed\" && $.ttft_ms = * }"

  metric_transformation {
    name      = "TimeToFirstToken"
    namespace = local.metrics_namespace
    value     = "$.ttft_ms"
    unit      = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "chunks" {
  name           = "${local.name}-chunks"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"retrieval.completed\" }"

  metric_transformation {
    name      = "ChunksRetrieved"
    namespace = local.metrics_namespace
    value     = "$.chunks"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "retrieval_latency" {
  name           = "${local.name}-retrieval-latency"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"retrieval.completed\" }"

  metric_transformation {
    name      = "RetrievalLatency"
    namespace = local.metrics_namespace
    value     = "$.latency_ms"
    unit      = "Milliseconds"
  }
}

# Una respuesta que afirma sin citar teniendo evidencia. Es el modo de falla que
# importa en este producto, y por eso tiene métrica propia.
resource "aws_cloudwatch_log_metric_filter" "grounding_failures" {
  name           = "${local.name}-grounding-failures"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"grounding.failure\" }"

  metric_transformation {
    name          = "GroundingFailures"
    namespace     = local.metrics_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "errors" {
  name           = "${local.name}-errors"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"response.failed\" || $.event = \"request.failed\" }"

  metric_transformation {
    name          = "Errors"
    namespace     = local.metrics_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "ttft_p95" {
  alarm_name          = "${local.name}-ttft-p95"
  alarm_description   = "El p95 del tiempo al primer token supera el presupuesto del contrato §8."
  namespace           = local.metrics_namespace
  metric_name         = "TimeToFirstToken"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.ttft_p95_alarm_seconds * 1000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching" # sin tráfico no hay incidente

  tags = { Name = "${local.name}-alarm-ttft" }
}

resource "aws_cloudwatch_metric_alarm" "grounding" {
  alarm_name          = "${local.name}-grounding-failures"
  alarm_description   = "El agente afirmó sin citar teniendo evidencia recuperada."
  namespace           = local.metrics_namespace
  metric_name         = "GroundingFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  tags = { Name = "${local.name}-alarm-grounding" }
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Tiempo al primer token (ms)"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            [local.metrics_namespace, "TimeToFirstToken", { stat = "p50", label = "p50" }],
            ["...", { stat = "p95", label = "p95" }],
          ]
          annotations = {
            horizontal = [{ label = "presupuesto §8", value = var.ttft_p95_alarm_seconds * 1000 }]
          }
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Recuperación"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            [local.metrics_namespace, "RetrievalLatency", { stat = "p95", label = "latencia p95" }],
            [local.metrics_namespace, "ChunksRetrieved", { stat = "Average", label = "fragmentos (media)" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Errores y fallos de fundamentación"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            [local.metrics_namespace, "Errors", { stat = "Sum" }],
            [local.metrics_namespace, "GroundingFailures", { stat = "Sum" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Servicio"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.api.name],
            [".", "MemoryUtilization", ".", ".", ".", "."],
          ]
        }
      },
    ]
  })
}
