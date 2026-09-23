# ============================================================
# Task registry "lambda" backend type -- plain AWS Lambda functions
# invoked directly by the main skill Lambda (via lambda.invoke) for
# named tasks that don't need AgentCore Runtime's containerized-
# agent machinery. See solution-design.md Section 11 for the task
# registry's three-backend-type design.
#
# GetWeather is the only task on this backend: a deterministic
# Open-Meteo API lookup that doesn't need AgentCore's reasoning or
# memory capabilities -- see lambda_tasks/get_weather/README.md.
#
# Each lambda-backend task gets its own function + its own minimal
# execution role, scoped to nothing but CloudWatch Logs -- these
# functions make outbound HTTPS calls to public APIs (Open-Meteo)
# and don't touch DynamoDB, S3, or Bedrock at all, so there is no
# reason to grant them anything beyond log write access.
# ============================================================

data "archive_file" "get_weather_lambda_src" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda_tasks/get_weather"
  output_path = "${path.module}/lambda_build/get_weather_task.zip"
  excludes = [
    "tests",
    "__pycache__",
    ".pytest_cache",
  ]
}

resource "aws_cloudwatch_log_group" "get_weather_task_logs" {
  name              = "/aws/lambda/${var.project_name}-get-weather-task-${var.environment}"
  retention_in_days = var.cloudwatch_log_retention_days
  tags              = var.tags
}

resource "aws_iam_role" "get_weather_task_exec" {
  name = "${var.project_name}-get-weather-task-exec-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "LambdaAssumeRole"
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "get_weather_task_logs" {
  name = "${var.project_name}-get-weather-task-logs-${var.environment}"
  role = aws_iam_role.get_weather_task_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteOwnLogGroup"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.get_weather_task_logs.arn}:*"
      }
    ]
  })
}

resource "aws_lambda_function" "get_weather_task" {
  function_name = "${var.project_name}-get-weather-task-${var.environment}"
  role          = aws_iam_role.get_weather_task_exec.arn
  handler       = "handler.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = 10 # a couple of quick outbound HTTPS calls, no Bedrock round trip
  memory_size   = 128

  filename         = data.archive_file.get_weather_lambda_src.output_path
  source_code_hash = data.archive_file.get_weather_lambda_src.output_base64sha256

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.get_weather_task_logs]
}

# NOTE: no aws_lambda_permission resource is needed here. Unlike the
# Alexa-facing permissions in lambda.tf (which grant a specific AWS
# *service principal*, alexa-appkit.amazon.com, permission to invoke
# a Lambda), this is a same-account Lambda-to-Lambda call. That kind
# of call is authorized purely by the CALLING Lambda's own IAM
# execution role having lambda:InvokeFunction on this function's
# ARN -- granted via aws_iam_role_policy.lambda_invoke_get_weather_task
# in iam.tf -- not by a resource-based policy on this function.
