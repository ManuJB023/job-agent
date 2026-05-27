# SES identities. After `terraform apply`, AWS sends a verification email
# to each address. You must click the link before SES will send from/to them.
#
# Sandbox note: until your AWS account is moved out of SES sandbox, you can
# only send to verified addresses. For a personal job-agent that's fine —
# you'll only send to yourself.

resource "aws_ses_email_identity" "sender" {
  email = var.sender_email
}

resource "aws_ses_email_identity" "recipient" {
  count = var.sender_email == var.notification_email ? 0 : 1
  email = var.notification_email
}
