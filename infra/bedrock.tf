# Knowledge Base sobre **S3 Vectors** (decisión B, cerrada).
#
# El vector store era la partida con más riesgo de costo del proyecto:
# OpenSearch Serverless factura OCUs corriendo de forma continua, existan o no
# consultas, y ese piso es independiente del tamaño del corpus. Para decenas de
# archivos pequeños habría sido, con diferencia, el mayor gasto — pagar una
# infraestructura de búsqueda dimensionada para millones de vectores con el fin
# de indexar diez documentos.
#
# S3 Vectors no tiene piso: se paga por almacenamiento y por consulta. Con este
# corpus el costo mensual es de centavos. A cambio se acepta menor rendimiento
# en consultas de altísimo volumen, que no es el caso de uso, y un ecosistema
# más joven. Si el corpus creciera a millones de vectores con consultas
# constantes, la comparación se invierte y OpenSearch vuelve a tener sentido.

resource "aws_s3_bucket" "corpus" {
  bucket        = "${var.project}-corpus-${var.aws_account_id}"
  force_destroy = true # entorno desechable; el corpus vive fuera de AWS también

  tags = { Name = "${local.name}-corpus" }
}

resource "aws_s3_bucket_public_access_block" "corpus" {
  bucket                  = aws_s3_bucket.corpus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Son documentos de identidad: que nadie pueda subirlos sin cifrar en tránsito.
data "aws_iam_policy_document" "corpus_tls" {
  statement {
    sid       = "SoloTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.corpus.arn, "${aws_s3_bucket.corpus.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  policy = data.aws_iam_policy_document.corpus_tls.json
}

# --- Almacén vectorial --------------------------------------------------------

resource "aws_s3vectors_vector_bucket" "kb" {
  vector_bucket_name = "${var.project}-vectors-${var.aws_account_id}"
  force_destroy      = true

  tags = { Name = "${local.name}-vectors" }
}

resource "aws_s3vectors_index" "kb" {
  vector_bucket_name = aws_s3vectors_vector_bucket.kb.vector_bucket_name
  index_name         = "${var.project}-index"
  data_type          = "float32"
  dimension          = var.embedding_dimension
  distance_metric    = "cosine"

  # Bedrock guarda el texto del fragmento y sus metadatos internos aquí. Deben
  # declararse no filtrables o la ingesta falla al superar el límite de
  # metadatos filtrables.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }

  tags = { Name = "${local.name}-index" }
}

# --- Rol de la Knowledge Base -------------------------------------------------

data "aws_iam_policy_document" "assume_bedrock_kb" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    # Sin estas condiciones el rol es asumible por el servicio en nombre de
    # cualquier cuenta: el problema del "confused deputy".
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.aws_account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:knowledge-base/*"]
    }
  }
}

resource "aws_iam_role" "knowledge_base" {
  name_prefix        = "${var.project}-kb-"
  assume_role_policy = data.aws_iam_policy_document.assume_bedrock_kb.json
  tags               = { Name = "${local.name}-kb-role" }
}

data "aws_iam_policy_document" "knowledge_base" {
  statement {
    sid       = "GenerarEmbeddings"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }

  statement {
    sid       = "LeerElCorpus"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.corpus.arn, "${aws_s3_bucket.corpus.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [var.aws_account_id]
    }
  }

  statement {
    sid = "EscribirYConsultarVectores"
    actions = [
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:QueryVectors",
      "s3vectors:ListVectors",
      "s3vectors:DeleteVectors",
    ]
    resources = [
      aws_s3vectors_vector_bucket.kb.vector_bucket_arn,
      aws_s3vectors_index.kb.index_arn,
    ]
  }
}

resource "aws_iam_role_policy" "knowledge_base" {
  name_prefix = "kb-"
  role        = aws_iam_role.knowledge_base.id
  policy      = data.aws_iam_policy_document.knowledge_base.json
}

# --- Knowledge Base y origen de datos -----------------------------------------

resource "aws_bedrockagent_knowledge_base" "main" {
  name     = "${var.project}-kb"
  role_arn = aws_iam_role.knowledge_base.arn

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
    }
  }

  storage_configuration {
    type = "S3_VECTORS"

    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb.index_arn
    }
  }

  tags = { Name = "${local.name}-kb" }

  depends_on = [aws_iam_role_policy.knowledge_base]
}

resource "aws_bedrockagent_data_source" "corpus" {
  knowledge_base_id    = aws_bedrockagent_knowledge_base.main.id
  name                 = "${var.project}-corpus"
  description          = "Documentos oficiales normalizados: títulos, cédulas, certificaciones y CV"
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn = aws_s3_bucket.corpus.arn
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      # Un documento = un fragmento (decisión de E2). Un título o una cédula
      # son cortos por naturaleza; el default de 300 tokens los parte y destroza
      # la relación entre institución, carrera y fecha. Es la causa número uno
      # de RAG malo sobre corpus pequeños.
      chunking_strategy = "NONE"
    }
  }
}
