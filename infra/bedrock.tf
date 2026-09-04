# Knowledge Base sobre **S3 Vectors**, una por tema (decisión B, cerrada).
#
# Topología: **un solo plano de cómputo, N bases de conocimiento**. La VPC, el
# balanceador, el servicio de ECS y los endpoints de interfaz —que son el costo
# fijo del despliegue— se comparten entre todos los temas; lo que se duplica es
# el índice vectorial, que sobre S3 Vectors cuesta centavos. Añadir un tema es
# crear su `profiles/<slug>.yaml` y aplicar: no otro balanceador.
#
# El corpus vive en un único bucket con un prefijo por tema. Un bucket por tema
# no aportaría aislamiento real —el mismo rol los leería todos— y multiplicaría
# políticas idénticas.
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

# Puede haber documentos sensibles: que nadie pueda subirlos sin cifrar en tránsito.
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

# Un índice por tema: mezclar corpus en un mismo índice haría competir en el
# ranking a documentos de dominios distintos, y un filtro mal puesto devolvería
# fichas de coches a una pregunta sobre credenciales.
resource "aws_s3vectors_index" "kb" {
  for_each = local.profiles

  vector_bucket_name = aws_s3vectors_vector_bucket.kb.vector_bucket_name
  index_name         = "${var.project}-${each.key}"
  data_type          = "float32"
  dimension          = var.embedding_dimension
  distance_metric    = "cosine"

  # Bedrock guarda el texto del fragmento y sus metadatos internos aquí. Deben
  # declararse no filtrables o la ingesta falla al superar el límite de
  # metadatos filtrables.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }

  tags = { Name = "${local.name}-${each.key}-index" }
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
    resources = concat(
      [aws_s3vectors_vector_bucket.kb.vector_bucket_arn],
      [for indice in aws_s3vectors_index.kb : indice.index_arn],
    )
  }
}

resource "aws_iam_role_policy" "knowledge_base" {
  name_prefix = "kb-"
  role        = aws_iam_role.knowledge_base.id
  policy      = data.aws_iam_policy_document.knowledge_base.json
}

# --- Knowledge Base y origen de datos -----------------------------------------

resource "aws_bedrockagent_knowledge_base" "main" {
  for_each = local.profiles

  name     = "${var.project}-${each.key}"
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
      index_arn = aws_s3vectors_index.kb[each.key].index_arn
    }
  }

  tags = { Name = "${local.name}-${each.key}-kb" }

  depends_on = [aws_iam_role_policy.knowledge_base]
}

resource "aws_bedrockagent_data_source" "corpus" {
  for_each = local.profiles

  knowledge_base_id    = aws_bedrockagent_knowledge_base.main[each.key].id
  name                 = "${var.project}-${each.key}-corpus"
  description          = try(each.value.name, each.key)
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn = aws_s3_bucket.corpus.arn
      # Cada tema ve solo su prefijo. Sin esto, las tres bases ingerirían el
      # corpus entero y cada una respondería con documentos de las otras.
      inclusion_prefixes = ["${each.key}/"]
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      # `NONE` en todos los temas: el troceado ya lo hizo `make corpus`, con la
      # política del perfil y produciendo `document_id` legibles. Delegarlo aquí
      # devolvería fragmentos citados por URI de S3 y sin los metadatos que el
      # pipeline deduce de la ruta.
      chunking_strategy = "NONE"
    }
  }
}
