output "jobs_table_name" {
  value = aws_dynamodb_table.jobs.name
}

output "collector_function_name" {
  value = aws_lambda_function.collector.function_name
}

output "scorer_function_name" {
  value = aws_lambda_function.scorer.function_name
}

output "notifier_function_name" {
  value = aws_lambda_function.notifier.function_name
}

output "next_steps" {
  value = <<-EOT
    Next steps:
      1. Verify SES identities by clicking the link in the email sent to:
         - sender:    ${var.sender_email}
         - recipient: ${var.notification_email}
      2. Run `make invoke-collector` to do a test ingest.
      3. Tail logs: `aws logs tail /aws/lambda/${aws_lambda_function.collector.function_name} --follow`
  EOT
}
