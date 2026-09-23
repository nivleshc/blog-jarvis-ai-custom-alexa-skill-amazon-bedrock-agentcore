# ============================================================
# AWS Budget -- the last-resort billing tripwire, independent of
# the CloudWatch EMF-based cost estimate in cloudwatch.tf.
#
# This reacts to actual AWS billing data (which has an inherent
# reporting delay, typically several hours), whereas the
# CloudWatch alarm reacts to the Lambda's own real-time estimate.
# The two are deliberately redundant -- see solution-design.md
# Section 8.5.
# ============================================================

resource "aws_budgets_budget" "monthly_cost" {
  name         = "${var.project_name}-${var.environment}-monthly-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # NOTE on the "$" escaping below: HCL treats "$$" as an escape for a
  # literal "${" sequence, which is NOT what's needed here. To emit a
  # literal "$" immediately followed by a real interpolation, the
  # `${"$"}` idiom is used instead -- this produces the AWS-required
  # "TagKey$TagValue" format, e.g. "user:Project$echo-show-bedrock-assistant".
  cost_filter {
    name = "TagKeyValue"
    values = [
      "user:Project${"$"}${var.project_name}"
    ]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
