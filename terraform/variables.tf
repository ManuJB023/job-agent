variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (prod, dev, etc.)"
  type        = string
  default     = "prod"
}

variable "notification_email" {
  description = "Email address to receive the daily digest. Must be SES-verified."
  type        = string
}

variable "sender_email" {
  description = "Verified SES sender identity."
  type        = string
}

variable "fit_score_threshold" {
  description = "Minimum fit score (0-100) for a job to appear in the digest."
  type        = number
  default     = 70
}

variable "collector_cron_expressions" {
  description = "EventBridge cron expressions for collector runs (UTC)."
  type        = list(string)
  # 12:00 UTC = 7am EST / 8am EDT. 00:00 UTC = 7pm EST / 8pm EDT.
  default = [
    "cron(0 12 * * ? *)",
    "cron(0 0 * * ? *)",
  ]
}

variable "notifier_cron_expressions" {
  description = "EventBridge cron expressions for notifier runs (UTC). Should be ~1 hour after collector."
  type        = list(string)
  default = [
    "cron(0 13 * * ? *)",
    "cron(0 1 * * ? *)",
  ]
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 14
}
