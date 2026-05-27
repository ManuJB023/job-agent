# ─────────────────────────────────────────────────────────────────────
# prod.example.tfvars
#
# Copy this file to prod.tfvars and replace the placeholder values
# with your own before running `make deploy`. The real prod.tfvars
# is gitignored — never commit real email addresses or other PII.
#
#   cp terraform/prod.example.tfvars terraform/prod.tfvars
#   $EDITOR terraform/prod.tfvars
#
# Both notification_email and sender_email must be SES-verified.
# Gmail is strongly recommended over ProtonMail — see README pre-
# deployment checklist for why.
# ─────────────────────────────────────────────────────────────────────

region              = "us-east-1"
environment         = "prod"
notification_email  = "your.email@gmail.com"
sender_email        = "your.email@gmail.com"
fit_score_threshold = 70
