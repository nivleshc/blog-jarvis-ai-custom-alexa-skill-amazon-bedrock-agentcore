# ============================================================
# Outputs -- values needed by later stages (Lambda environment
# variables, IAM policy scoping, operational scripts) and useful
# for verifying what was deployed.
# ============================================================

output "skill_users_table_name" {
  description = "Name of the SkillUsers (allowlist) DynamoDB table"
  value       = aws_dynamodb_table.skill_users.name
}

output "skill_users_table_arn" {
  description = "ARN of the SkillUsers (allowlist) DynamoDB table"
  value       = aws_dynamodb_table.skill_users.arn
}

output "skill_usage_counters_table_name" {
  description = "Name of the SkillUsageCounters (per-user rate limit) DynamoDB table"
  value       = aws_dynamodb_table.skill_usage_counters.name
}

output "skill_usage_counters_table_arn" {
  description = "ARN of the SkillUsageCounters (per-user rate limit) DynamoDB table"
  value       = aws_dynamodb_table.skill_usage_counters.arn
}

output "global_usage_counters_table_name" {
  description = "Name of the GlobalUsageCounters (aggregate ceiling) DynamoDB table"
  value       = aws_dynamodb_table.global_usage_counters.name
}

output "global_usage_counters_table_arn" {
  description = "ARN of the GlobalUsageCounters (aggregate ceiling) DynamoDB table"
  value       = aws_dynamodb_table.global_usage_counters.arn
}

output "skill_call_log_table_name" {
  description = "Name of the SkillCallLog (per-call audit) DynamoDB table"
  value       = aws_dynamodb_table.skill_call_log.name
}

output "skill_call_log_table_arn" {
  description = "ARN of the SkillCallLog (per-call audit) DynamoDB table"
  value       = aws_dynamodb_table.skill_call_log.arn
}

output "lambda_log_group_name" {
  description = "Name of the CloudWatch Log Group the Lambda function will write to (created here so retention is set before the function exists)"
  value       = aws_cloudwatch_log_group.lambda_logs.name
}

output "sns_alerts_topic_arn" {
  description = "ARN of the shared SNS topic used by CloudWatch alarms and the AWS Budget notification"
  value       = aws_sns_topic.alerts.arn
}

output "cloudwatch_dashboard_name" {
  description = "Name of the CloudWatch Dashboard"
  value       = aws_cloudwatch_dashboard.main.dashboard_name
}

output "emf_namespace" {
  description = "CloudWatch metric namespace the Lambda's EMF log lines will publish under. Needed by the Lambda's environment variables."
  value       = local.emf_namespace
}

output "budget_name" {
  description = "Name of the AWS Budget monthly cost tripwire"
  value       = aws_budgets_budget.monthly_cost.name
}

output "lambda_function_name" {
  description = "Name of the deployed Lambda function"
  value       = aws_lambda_function.alexa_skill.function_name
}

output "lambda_function_arn" {
  description = "ARN of the deployed Lambda function -- this is the value the Alexa skill manifest's endpoint URI must point to"
  value       = aws_lambda_function.alexa_skill.arn
}

output "lambda_exec_role_arn" {
  description = "ARN of the Lambda's least-privilege execution role"
  value       = aws_iam_role.lambda_exec.arn
}

output "task_registry_bucket_name" {
  description = "Name of the S3 bucket hosting task_registry.json"
  value       = aws_s3_bucket.task_registry.id
}

output "task_registry_key" {
  description = "S3 object key of the task registry file"
  value       = local.task_registry_key
}

output "skill_icon_small_uri" {
  description = "Public HTTPS URL of the 108x108 small skill icon -- passed to deploy_skill.py to populate the manifest's smallIconUri"
  value       = local.small_icon_uri
}

output "skill_icon_large_uri" {
  description = "Public HTTPS URL of the 512x512 large skill icon -- passed to deploy_skill.py to populate the manifest's largeIconUri"
  value       = local.large_icon_uri
}

output "get_weather_task_lambda_arn" {
  description = "ARN of the GetWeather task's Lambda function (the 'lambda' task-registry backend type). Automatically wired into task_registry.json's GetWeather.lambda_function_arn -- see terraform/s3.tf's templatefile() rendering. Exposed as an output mainly for verification/debugging, not because anything needs to read it manually."
  value       = aws_lambda_function.get_weather_task.arn
}

output "boredom_buster_memory_id" {
  description = "ID of the Boredom Buster AgentCore Memory resource. Automatically wired into the deployed agent's BEDROCK_AGENTCORE_MEMORY_ID environment variable by terraform/agentcore_runtime.tf -- exposed here mainly for verification/debugging, not because anything needs to read it manually."
  value       = aws_bedrockagentcore_memory.boredom_buster.id
}

output "boredom_buster_memory_arn" {
  description = "ARN of the Boredom Buster AgentCore Memory resource."
  value       = aws_bedrockagentcore_memory.boredom_buster.arn
}

output "tmdb_api_key_parameter_name" {
  description = "Name of the SSM Parameter Store parameter holding the TMDB API key. Automatically wired into the deployed agent's TMDB_API_KEY_PARAMETER_NAME environment variable by terraform/agentcore_runtime.tf -- the agent reads the actual value from SSM at request time, it is never placed in an environment variable directly. Exposed here for verification, e.g. `aws ssm get-parameter --name $(terraform output -raw tmdb_api_key_parameter_name) --with-decryption`."
  value       = aws_ssm_parameter.tmdb_api_key.name
}

output "boredom_buster_agent_runtime_arn" {
  description = "ARN of the deployed Boredom Buster AgentCore Runtime agent. Automatically wired into task_registry.json's BoredomBuster.agent_runtime_arn -- see terraform/s3.tf's templatefile() rendering. Exposed as an output mainly for verification/debugging."
  value       = aws_bedrockagentcore_agent_runtime.boredom_buster.agent_runtime_arn
}

output "boredom_buster_agent_runtime_id" {
  description = "ID of the deployed Boredom Buster AgentCore Runtime agent -- useful for `aws bedrock-agentcore-control get-agent-runtime` or console lookups."
  value       = aws_bedrockagentcore_agent_runtime.boredom_buster.agent_runtime_id
}
