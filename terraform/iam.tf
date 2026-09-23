# ============================================================
# IAM -- least-privilege Lambda execution role.
#
# References the Lambda function's own log group and the DynamoDB
# table ARNs, defined elsewhere in this Terraform config.
#
# Scoped per solution-design.md's repeated emphasis on avoiding
# broad policies like AmazonBedrockFullAccess -- every statement
# below is scoped to specific resources, not "*", except where AWS
# APIs genuinely require a wildcard (e.g. DynamoDB table creation
# isn't in scope here, only item-level operations on named tables).
# ============================================================

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}

resource "aws_iam_role" "lambda_exec" {
  name = "${var.project_name}-lambda-exec-${var.environment}"

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

# ------------------------------------------------------------
# CloudWatch Logs -- write access scoped to this function's own
# log group only (created in cloudwatch.tf).
# ------------------------------------------------------------
resource "aws_iam_role_policy" "lambda_logs" {
  name = "${var.project_name}-lambda-logs-${var.environment}"
  role = aws_iam_role.lambda_exec.id

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
        Resource = "${aws_cloudwatch_log_group.lambda_logs.arn}:*"
      }
    ]
  })
}

# ------------------------------------------------------------
# DynamoDB -- item-level access only, scoped to this project's 5
# tables. No CreateTable/DeleteTable/scan-everything permissions.
# ------------------------------------------------------------
resource "aws_iam_role_policy" "lambda_dynamodb" {
  name = "${var.project_name}-lambda-dynamodb-${var.environment}"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowlistReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ]
        Resource = aws_dynamodb_table.skill_users.arn
      },
      {
        Sid    = "RateLimitCountersReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
        ]
        Resource = [
          aws_dynamodb_table.skill_usage_counters.arn,
          aws_dynamodb_table.global_usage_counters.arn,
        ]
      },
      {
        Sid    = "CallLogWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
        ]
        Resource = aws_dynamodb_table.skill_call_log.arn
      },
      {
        Sid    = "WatchlistReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
        ]
        Resource = aws_dynamodb_table.skill_watchlist.arn
      }
    ]
  })
}

# ------------------------------------------------------------
# Bedrock -- scoped to the specific Nova model used for free-form
# Q&A, plus AgentCore Runtime invocation. Deliberately does NOT
# grant bedrock:* or a wildcard model resource -- see
# solution-design.md Section 7 for why this matters.
#
# The foundation-model resource ARN pattern uses a wildcard region/
# account because Bedrock foundation models are not account-owned
# resources -- this is the AWS-documented ARN shape for on-demand
# model invocation, not a broad permission grant.
# ------------------------------------------------------------
resource "aws_iam_role_policy" "lambda_bedrock" {
  name = "${var.project_name}-lambda-bedrock-${var.environment}"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeNovaFoundationModel"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}"
      },
      {
        Sid    = "InvokeAgentCoreRuntime"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        # Scoped to this account/region's AgentCore Runtime resources only.
        # Specific per-agent ARNs are intentionally not enumerated here
        # since the task registry (S3-hosted) is the source of truth for
        # which agents exist and can change without a Terraform apply --
        # see solution-design.md Section 11 on hot-reload extensibility.
        Resource = "arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${local.account_id}:runtime/*"
      }
    ]
  })
}

# ------------------------------------------------------------
# Lambda-to-Lambda invocation -- the "lambda" task registry backend
# type (terraform/lambda_tasks.tf). Scoped to the specific task
# Lambda function ARN(s), not lambda:InvokeFunction on "*" -- same
# least-privilege principle as every other statement in this file.
# ------------------------------------------------------------
resource "aws_iam_role_policy" "lambda_invoke_task_functions" {
  name = "${var.project_name}-lambda-invoke-task-functions-${var.environment}"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeGetWeatherTask"
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction",
        ]
        Resource = aws_lambda_function.get_weather_task.arn
      }
    ]
  })
}

# ------------------------------------------------------------
# S3 -- read-only access to the task registry object, not the
# whole bucket. See s3.tf for the bucket itself.
# ------------------------------------------------------------
resource "aws_iam_role_policy" "lambda_s3_task_registry" {
  name = "${var.project_name}-lambda-s3-task-registry-${var.environment}"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadTaskRegistryObject"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
        ]
        Resource = "${aws_s3_bucket.task_registry.arn}/${local.task_registry_key}"
      }
    ]
  })
}
