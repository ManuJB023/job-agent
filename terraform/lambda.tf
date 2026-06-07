# ─────────────────────────────────────────────────────────────────────────────
# Packaging
# Each Lambda is zipped from its source directory plus the shared/ module.
# In CI we install deps into a "build" subdirectory first; locally `make build`
# does the same. terraform expects the build directory to already exist.
# ─────────────────────────────────────────────────────────────────────────────
data "archive_file" "collector_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../build/collector"
  output_path = "${path.module}/../build/collector.zip"
}

data "archive_file" "scorer_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../build/scorer"
  output_path = "${path.module}/../build/scorer.zip"
}

data "archive_file" "notifier_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../build/notifier"
  output_path = "${path.module}/../build/notifier.zip"
}

resource "aws_lambda_layer_version" "pandas_layer" {
  s3_bucket           = aws_s3_bucket.artifacts.id
  s3_key              = aws_s3_object.pandas_layer_zip.key
  layer_name          = "${local.name_prefix}-pandas"
  compatible_runtimes = ["python3.12"]
  source_code_hash    = filebase64sha256("../build/pandas_layer.zip")
}

# ─────────────────────────────────────────────────────────────────────────────
# Log groups (explicit so retention is controlled)
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "collector" {
  name              = "/aws/lambda/${local.name_prefix}-collector"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "scorer" {
  name              = "/aws/lambda/${local.name_prefix}-scorer"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${local.name_prefix}-notifier"
  retention_in_days = var.log_retention_days
}

# ─────────────────────────────────────────────────────────────────────────────
# Functions
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_lambda_function" "collector" {
  function_name    = "${local.name_prefix}-collector"
  role             = aws_iam_role.collector.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.collector_zip.key
  source_code_hash = data.archive_file.collector_zip.output_base64sha256
  timeout          = 600  # JobSpy across multiple sources can be slow
  memory_size      = 1024
  layers           = [aws_lambda_layer_version.pandas_layer.arn, aws_lambda_layer_version.tls_layer.arn]

  environment {
    variables = {
      JOBS_TABLE   = aws_dynamodb_table.jobs.name
      LOG_LEVEL    = "INFO"
      JSEARCH_PARAM = "/job-agent/jsearch_api_key"
    }
  }

  depends_on = [aws_cloudwatch_log_group.collector]
}

resource "aws_lambda_function" "scorer" {
  function_name    = "${local.name_prefix}-scorer"
  role             = aws_iam_role.scorer.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.scorer_zip.key
  source_code_hash = data.archive_file.scorer_zip.output_base64sha256
  timeout          = 120
  memory_size      = 512

  environment {
    variables = {
      JOBS_TABLE          = aws_dynamodb_table.jobs.name
      LOG_LEVEL           = "INFO"
      ANTHROPIC_API_KEY_PARAM = "/job-agent/anthropic_api_key"
      ANTHROPIC_MODEL     = "claude-haiku-4-5-20251001"
    }
  }

  depends_on = [aws_cloudwatch_log_group.scorer]
}

resource "aws_lambda_function" "notifier" {
  function_name    = "${local.name_prefix}-notifier"
  role             = aws_iam_role.notifier.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.notifier_zip.key
  source_code_hash = data.archive_file.notifier_zip.output_base64sha256
  timeout          = 60
  memory_size      = 512

  environment {
    variables = {
      JOBS_TABLE          = aws_dynamodb_table.jobs.name
      LOG_LEVEL           = "INFO"
      NOTIFICATION_EMAIL  = var.notification_email
      SENDER_EMAIL        = var.sender_email
      FIT_SCORE_THRESHOLD = tostring(var.fit_score_threshold)
    }
  }

  depends_on = [aws_cloudwatch_log_group.notifier]
}

# ─────────────────────────────────────────────────────────────────────────────
# Stream → scorer
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_lambda_event_source_mapping" "scorer_stream" {
  event_source_arn  = aws_dynamodb_table.jobs.stream_arn
  function_name     = aws_lambda_function.scorer.arn
  starting_position = "LATEST"
  batch_size        = 5
  maximum_retry_attempts = 2

  filter_criteria {
    filter {
      # Only fire for INSERT events (not modifications by the scorer itself).
      pattern = jsonencode({
        eventName = ["INSERT"]
      })
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Error alarms
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "collector_errors" {
  alarm_name          = "${local.name_prefix}-collector-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.collector.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "scorer_errors" {
  alarm_name          = "${local.name_prefix}-scorer-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.scorer.function_name
  }
}

resource "aws_s3_object" "tls_layer_zip" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "tls_layer.zip"
  source = "../build/tls_layer.zip"
  etag   = filemd5("../build/tls_layer.zip")
}

resource "aws_lambda_layer_version" "tls_layer" {
  s3_bucket           = aws_s3_bucket.artifacts.id
  s3_key              = aws_s3_object.tls_layer_zip.key
  layer_name          = "${local.name_prefix}-tls"
  compatible_runtimes = ["python3.12"]
  source_code_hash    = filebase64sha256("../build/tls_layer.zip")
}