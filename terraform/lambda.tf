# ============================================================
# Lambda function -- the skill's backend.
#
# Packages and deploys alexa_lambda/ as-is: the allowlist check,
# rate limiting, Bedrock/AgentCore routing, and observability
# writes all live in that directory's handler code (see
# handlers/handler.py), not in this Terraform config.
# ============================================================

data "archive_file" "lambda_src" {
  type        = "zip"
  source_dir  = "${path.module}/../alexa_lambda"
  output_path = "${path.module}/lambda_build/alexa_lambda.zip"
  excludes = [
    "tests",
    "__pycache__",
    ".pytest_cache",
  ]
}

resource "aws_lambda_function" "alexa_skill" {
  function_name = "${var.project_name}-${var.environment}"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "handlers.handler.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_size

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      ALEXA_SKILL_ID = var.alexa_skill_id
      ENVIRONMENT    = var.environment

      # Model / cost controls -- see solution-design.md Section 7
      BEDROCK_MODEL_ID   = var.bedrock_model_id
      BEDROCK_MAX_TOKENS = tostring(var.bedrock_max_tokens)
      MAX_INPUT_CHARS    = tostring(var.max_input_chars)

      INPUT_PRICE_PER_MILLION_TOKENS  = tostring(var.input_price_per_million_tokens)
      OUTPUT_PRICE_PER_MILLION_TOKENS = tostring(var.output_price_per_million_tokens)

      # Rate limiting -- see solution-design.md Section 6
      GLOBAL_DEFAULT_DAILY_LIMIT = tostring(var.global_default_daily_limit)
      GLOBAL_DEFAULT_BURST_LIMIT = tostring(var.global_default_burst_limit)
      GLOBAL_DAILY_CEILING       = tostring(var.global_daily_ceiling)
      USAGE_COUNTER_TTL_DAYS     = tostring(var.usage_counter_ttl_days)
      CALL_LOG_TTL_DAYS          = tostring(var.call_log_ttl_days)

      # DynamoDB table names -- see solution-design.md Section 5
      SKILL_USERS_TABLE           = aws_dynamodb_table.skill_users.name
      SKILL_USAGE_COUNTERS_TABLE  = aws_dynamodb_table.skill_usage_counters.name
      GLOBAL_USAGE_COUNTERS_TABLE = aws_dynamodb_table.global_usage_counters.name
      SKILL_CALL_LOG_TABLE        = aws_dynamodb_table.skill_call_log.name
      SKILL_WATCHLIST_TABLE       = aws_dynamodb_table.skill_watchlist.name

      # Task registry -- see solution-design.md Section 11
      TASK_REGISTRY_BUCKET = aws_s3_bucket.task_registry.id
      TASK_REGISTRY_KEY    = local.task_registry_key

      # Observability -- see solution-design.md Section 8
      EMF_NAMESPACE = local.emf_namespace
    }
  }

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.lambda_logs]
}

# ------------------------------------------------------------
# Permission for Alexa to invoke this Lambda.
#
# THE BOOTSTRAP PROBLEM: SMAPI validates that a Lambda already has
# an Alexa-invoke permission ("trigger") BEFORE it will accept a
# skill manifest pointing at that Lambda's ARN -- confirmed against
# Amazon's own docs ("Host a Custom Skill as an AWS Lambda
# Function": "You must configure at least one trigger for your
# function to grant Alexa the necessary invocation permissions").
# Without ANY permission present, skill creation fails at import
# with "The trigger setting for the Lambda ... is invalid."
#
# But the fully-scoped permission below (with event_source_token)
# can only be created once alexa_skill_id is known -- which it
# isn't, on a brand-new deployment, until AFTER the first SMAPI
# import succeeds. That's a genuine circular dependency, not
# something resolvable by reordering resources.
#
# Amazon's own documentation describes the sanctioned way out of
# this: create the permission WITHOUT event_source_token first
# (skill ID verification disabled -- allows any Alexa skill to
# invoke the function), then replace it with the scoped version
# once the skill ID is known: "It is still recommended that you
# re-enable verification before publishing your skill."
#
# These two resources are mutually exclusive via count -- exactly
# one of them exists at any time, driven by whether alexa_skill_id
# is set yet. See DEPLOYMENT.md's "Deploy" section for the resulting
# 2-apply sequence this requires on a brand-new deployment.
# ------------------------------------------------------------

# Bootstrap permission -- exists ONLY while alexa_skill_id is still
# empty (i.e. before the skill has ever been created). Unscoped: any
# Alexa skill could technically invoke this Lambda while this is the
# active permission, which is why it is destroyed the moment
# alexa_skill_id is set -- see aws_lambda_permission.alexa_invoke_scoped
# below, which takes over from that point on.
resource "aws_lambda_permission" "alexa_invoke_bootstrap" {
  count = var.alexa_skill_id == "" ? 1 : 0

  statement_id  = "AllowAlexaSkillInvokeBootstrap"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alexa_skill.function_name
  principal     = "alexa-appkit.amazon.com"
}

# Scoped permission -- the "skill authenticity" defense-in-depth
# layer from solution-design.md Section 4, step 1. Takes over from
# the bootstrap permission once alexa_skill_id is known. Restricts
# invocation to this exact skill ID via event_source_token -- this
# is the permission that should exist for the lifetime of the
# deployment after initial setup.
resource "aws_lambda_permission" "alexa_invoke_scoped" {
  count = var.alexa_skill_id != "" ? 1 : 0

  statement_id       = "AllowAlexaSkillInvoke"
  action             = "lambda:InvokeFunction"
  function_name      = aws_lambda_function.alexa_skill.function_name
  principal          = "alexa-appkit.amazon.com"
  event_source_token = var.alexa_skill_id
}
