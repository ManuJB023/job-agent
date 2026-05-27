resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_object" "collector_zip" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "collector.zip"
  source = data.archive_file.collector_zip.output_path
  etag   = data.archive_file.collector_zip.output_md5
}

resource "aws_s3_object" "scorer_zip" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "scorer.zip"
  source = data.archive_file.scorer_zip.output_path
  etag   = data.archive_file.scorer_zip.output_md5
}

resource "aws_s3_object" "notifier_zip" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "notifier.zip"
  source = data.archive_file.notifier_zip.output_path
  etag   = data.archive_file.notifier_zip.output_md5
}

resource "aws_s3_object" "pandas_layer_zip" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "pandas_layer.zip"
  source = "../build/pandas_layer.zip"
  etag   = filemd5("../build/pandas_layer.zip")
}