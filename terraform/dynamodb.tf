resource "aws_dynamodb_table" "jobs" {
  name         = "${local.name_prefix}-jobs"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"
  range_key = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "fit_score"
    type = "N"
  }

  # GSI used by the notifier to fetch scored jobs above threshold,
  # ordered by score descending.
  global_secondary_index {
    name            = "status-fit_score-index"
    hash_key        = "status"
    range_key       = "fit_score"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Stream is what triggers the scorer Lambda when a new job lands.
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  point_in_time_recovery {
    enabled = false # personal tool; flip to true if data becomes precious
  }
}
