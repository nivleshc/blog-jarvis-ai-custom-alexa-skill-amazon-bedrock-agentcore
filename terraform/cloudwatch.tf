# ============================================================
# CloudWatch -- log group, EMF metric namespace, dashboard, and
# alarms. See solution-design.md Section 8 for the full
# observability design and what each piece answers.
#
# The Lambda emits Embedded Metric Format (EMF) log lines to its
# own log group; CloudWatch automatically extracts these into real
# metrics under the namespace below -- no separate PutMetricData
# calls or extra service cost beyond normal Lambda logging.
# ============================================================

locals {
  emf_namespace = "${var.project_name}/${var.environment}"
}

# ------------------------------------------------------------
# SNS topic -- shared destination for all alarms and the AWS Budget
# alert in budget.tf. One topic, one subscription, one place to
# manage who gets notified.
# ------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts-${var.environment}"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ------------------------------------------------------------
# Lambda log group -- created explicitly (rather than left to
# auto-create on first invocation) so the retention policy is set
# from day one and so EMF metrics have a group to attach to.
# ------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${var.project_name}-${var.environment}"
  retention_in_days = var.cloudwatch_log_retention_days
  tags              = var.tags
}

# ------------------------------------------------------------
# Dashboard -- see solution-design.md Section 8.3 for the panel
# list this implements: daily cost, requests split by outcome,
# per-user volume, error rate / latency.
# ------------------------------------------------------------
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project_name}-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Daily Estimated Cost (USD)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.emf_namespace, "EstimatedCostUSD", { stat = "Sum", period = 86400, label = "Estimated cost per day" }]
          ]
          yAxis = {
            left = { min = 0 }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Requests by Outcome (allowed vs. denied)"
          view    = "timeSeries"
          region  = var.aws_region
          stacked = true
          metrics = [
            [local.emf_namespace, "RequestsAllowed", { stat = "Sum", period = 3600, label = "Allowed" }],
            [local.emf_namespace, "RequestsDenied", { stat = "Sum", period = 3600, label = "Denied (all reasons)" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Denials by Reason"
          view    = "timeSeries"
          region  = var.aws_region
          stacked = true
          metrics = [
            [local.emf_namespace, "RequestsDenied", "DenialReason", "not_approved", { stat = "Sum", period = 3600, label = "Not approved" }],
            [local.emf_namespace, "RequestsDenied", "DenialReason", "daily_limit", { stat = "Sum", period = 3600, label = "Per-user daily limit" }],
            [local.emf_namespace, "RequestsDenied", "DenialReason", "burst_limit", { stat = "Sum", period = 3600, label = "Per-user burst limit" }],
            [local.emf_namespace, "RequestsDenied", "DenialReason", "global_ceiling", { stat = "Sum", period = 3600, label = "Global ceiling" }],
            [local.emf_namespace, "RequestsDenied", "DenialReason", "input_too_long", { stat = "Sum", period = 3600, label = "Input too long" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Top Users by Request Volume"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.emf_namespace, "RequestsAllowed", "userId", "ANY", { stat = "Sum", period = 3600 }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Lambda Errors"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project_name}-${var.environment}", { stat = "Sum", period = 300 }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Lambda Duration (p99)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project_name}-${var.environment}", { stat = "p99", period = 300 }]
          ]
        }
      }
    ]
  })
}

# ------------------------------------------------------------
# Alarms -- see solution-design.md Section 8.4.
# ------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "daily_cost_threshold" {
  alarm_name          = "${var.project_name}-${var.environment}-daily-cost-threshold"
  alarm_description   = "Fires when the Lambda's own EMF-derived daily cost estimate exceeds the configured threshold. This is an early-warning signal computed by the Lambda itself, distinct from the AWS Budget alarm which reacts to actual AWS billing data with inherent delay."
  namespace           = local.emf_namespace
  metric_name         = "EstimatedCostUSD"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.daily_cost_alarm_threshold_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "denial_rate_spike" {
  alarm_name          = "${var.project_name}-${var.environment}-denial-rate-spike"
  alarm_description   = "Fires when denied requests (allowlist rejections + rate-limit rejections combined) spike in a 5-minute window. Distinguishes abuse/bug signals from normal usage growth."
  namespace           = local.emf_namespace
  metric_name         = "RequestsDenied"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.denial_rate_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_error_rate" {
  alarm_name        = "${var.project_name}-${var.environment}-lambda-error-rate"
  alarm_description = "Fires when the Lambda's error count spikes in a 5-minute window."
  namespace         = "AWS/Lambda"
  metric_name       = "Errors"
  dimensions = {
    FunctionName = "${var.project_name}-${var.environment}"
  }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.lambda_error_rate_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
