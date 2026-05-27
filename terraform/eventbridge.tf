resource "aws_cloudwatch_event_rule" "collector" {
  count               = length(var.collector_cron_expressions)
  name                = "${local.name_prefix}-collector-${count.index}"
  description         = "Trigger collector Lambda (${count.index})"
  schedule_expression = var.collector_cron_expressions[count.index]
}

resource "aws_cloudwatch_event_target" "collector" {
  count = length(var.collector_cron_expressions)
  rule  = aws_cloudwatch_event_rule.collector[count.index].name
  arn   = aws_lambda_function.collector.arn
}

resource "aws_lambda_permission" "collector_eventbridge" {
  count         = length(var.collector_cron_expressions)
  statement_id  = "AllowEventBridgeInvoke-${count.index}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collector[count.index].arn
}

# ─────────────────────────────────────────────────────────────────────────────
# Notifier schedule
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "notifier" {
  count               = length(var.notifier_cron_expressions)
  name                = "${local.name_prefix}-notifier-${count.index}"
  description         = "Trigger notifier Lambda (${count.index})"
  schedule_expression = var.notifier_cron_expressions[count.index]
}

resource "aws_cloudwatch_event_target" "notifier" {
  count = length(var.notifier_cron_expressions)
  rule  = aws_cloudwatch_event_rule.notifier[count.index].name
  arn   = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "notifier_eventbridge" {
  count         = length(var.notifier_cron_expressions)
  statement_id  = "AllowEventBridgeInvoke-${count.index}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.notifier[count.index].arn
}
