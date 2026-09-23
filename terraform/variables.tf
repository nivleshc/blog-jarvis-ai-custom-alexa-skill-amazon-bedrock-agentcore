# ============================================================
# Core project variables
# ============================================================

variable "aws_region" {
  description = "AWS region to deploy all resources into. Bedrock AgentCore and Nova models must be available in this region."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming and tagging"
  type        = string
  default     = "echo-show-jarvis-ai"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "tags" {
  description = "Additional tags to apply to all resources, merged with the default_tags on the provider"
  type        = map(string)
  default     = {}
}

# ============================================================
# Lambda configuration
# ============================================================

variable "lambda_runtime" {
  description = "Lambda runtime version"
  type        = string
  default     = "python3.12"
}

variable "lambda_timeout" {
  description = "Lambda function timeout in seconds. Must accommodate a Bedrock/AgentCore round trip."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Lambda function memory size in MB"
  type        = number
  default     = 256
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrency for the Lambda function. Caps how many invocations can run in parallel across ALL users combined, as a backstop against coordinated bursts. Set to -1 to disable (unreserved, uses the account's unreserved pool)."
  type        = number
  default     = 10
}

variable "cloudwatch_log_retention_days" {
  description = "CloudWatch Logs retention in days for the Lambda's log group"
  type        = number
  default     = 30
}

# ============================================================
# Alexa Skill
# ============================================================

variable "alexa_skill_id" {
  description = "Alexa Skill ID from the Alexa Developer Console (amzn1.ask.skill.xxxxx...). Required for the aws_lambda_permission event_source_token and the applicationId check in code. Leave empty until the skill has been created via scripts/deploy_skill.py, then set it here."
  type        = string
  default     = ""
}

# ============================================================
# Bedrock model configuration
# ============================================================

variable "bedrock_model_id" {
  description = "Bedrock model ID used for free-form Q&A (the AskBedrock intent). Default is Amazon Nova Lite -- see solution-design.md Section 7.0.1. Must be an Amazon-owned model; third-party marketplace models (anthropic.*, meta.*, mistral.*, etc.) are out of scope for this project."
  type        = string
  default     = "amazon.nova-lite-v1:0"

  validation {
    condition     = can(regex("^amazon\\.", var.bedrock_model_id))
    error_message = "bedrock_model_id must be an Amazon-owned model (must start with \"amazon.\"). Third-party marketplace models (Anthropic, Meta, Mistral, Cohere, AI21, etc.) are out of scope for this project."
  }
}

variable "bedrock_max_tokens" {
  description = "Hard output token cap passed to every invoke_model / invoke_agent_runtime call. Enforced server-side by Bedrock -- this is one of the two deterministic cost controls in solution-design.md Section 7.1."
  type        = number
  default     = 512
}

variable "tmdb_watch_region" {
  description = "ISO-3166-1 country code (e.g. AU, US, GB) used for Boredom Buster's 'where to watch' lookups on the detail screen. Streaming availability differs per country, so TMDB's watch-providers API has no global answer -- set this to where the Echo Show actually is. Costs nothing either way; it is only a query parameter."
  type        = string
  default     = "AU"

  validation {
    condition     = can(regex("^[A-Za-z]{2}$", var.tmdb_watch_region))
    error_message = "tmdb_watch_region must be a two-letter ISO-3166-1 country code, e.g. AU."
  }
}

variable "input_price_per_million_tokens" {
  description = "Nova Lite on-demand input price per 1M tokens, used by cost.py's actual-cost calculation (not the pre-call character estimate). See solution-design.md Section 7.0.2. Re-verify against aws.amazon.com/bedrock/pricing/ periodically -- Bedrock pricing changes over time and this value will go stale if not updated."
  type        = number
  default     = 0.06
}

variable "output_price_per_million_tokens" {
  description = "Nova Lite on-demand output price per 1M tokens. See input_price_per_million_tokens's description for the same re-verification caveat."
  type        = number
  default     = 0.24
}

variable "max_input_chars" {
  description = "Hard input character cap, checked in Lambda code before any Bedrock/AgentCore request is constructed. The other of the two deterministic cost controls in solution-design.md Section 7.1."
  type        = number
  default     = 800
}

# ============================================================
# Rate limiting -- global defaults and aggregate ceiling
# (per-user overrides live in DynamoDB, not here -- see SkillUsers table)
# ============================================================

variable "global_default_daily_limit" {
  description = "Default per-user daily request limit, applied to every approved user unless they have a daily_limit_override set on their SkillUsers row. See solution-design.md Section 6."
  type        = number
  default     = 20
}

variable "global_default_burst_limit" {
  description = "Default per-user hourly burst request limit, applied unless overridden per-user. See solution-design.md Section 6. 20/hour bounds worst-case cost while allowing normal single-user use -- BoredomBuster's multi-turn clarifying-question flow alone can consume several requests per conversation. Use scripts/approve_user.py's --set-burst-limit to override per-user instead of raising this global default."
  type        = number
  default     = 20
}

variable "global_daily_ceiling" {
  description = "Aggregate cap on total requests across ALL approved users combined, per day. Independent of per-user limits -- protects against the sum of many well-behaved users still costing more than desired. See solution-design.md Section 6."
  type        = number
  default     = 200
}

# ============================================================
# DynamoDB table TTLs
# ============================================================

variable "usage_counter_ttl_days" {
  description = "TTL (in days) for items in SkillUsageCounters and GlobalUsageCounters. Old counters auto-expire via DynamoDB TTL -- no cleanup job needed."
  type        = number
  default     = 2
}

variable "call_log_ttl_days" {
  description = "TTL (in days) for items in SkillCallLog. Set to 0 to disable TTL entirely (keep call history forever) -- the default is 0 because historical cost/usage data is valuable and storage cost at this volume is trivial. See solution-design.md Section 5.4."
  type        = number
  default     = 0
}

# ============================================================
# Cost observability -- CloudWatch alarms and AWS Budget
# ============================================================

variable "alert_email" {
  description = "Email address to receive SNS notifications for CloudWatch alarms and the AWS Budget alert. Required -- there is no useful default."
  type        = string
}

variable "daily_cost_alarm_threshold_usd" {
  description = "CloudWatch alarm threshold, in USD, for the daily estimated cost EMF metric. Should be set comfortably above the worst-case daily figure computed in solution-design.md Section 7.3, so it only fires on genuine anomalies."
  type        = number
  default     = 5
}

variable "denial_rate_alarm_threshold" {
  description = "CloudWatch alarm threshold for the count of denied requests (allowlist/rate-limit rejections) in a 5-minute period. A spike here can indicate abuse attempts or a misbehaving client, distinct from the AWS Budget which only reacts to actual billing."
  type        = number
  default     = 50
}

variable "lambda_error_rate_alarm_threshold" {
  description = "CloudWatch alarm threshold for Lambda error count in a 5-minute period."
  type        = number
  default     = 5
}

variable "monthly_budget_usd" {
  description = "AWS Budget monthly limit, in USD, for the last-resort billing tripwire. This is independent of the CloudWatch-based cost estimates -- it reacts to actual AWS billing data (with inherent delay), not the Lambda's own running estimate."
  type        = number
  default     = 25
}

# ============================================================
# Boredom Buster -- TMDB API key
# ============================================================

variable "tmdb_api_key" {
  description = "TMDB (themoviedb.org) API key, used by the Boredom Buster AgentCore agent's search_movies/search_tv_shows tools. Get a free key at themoviedb.org/settings/api. Stored in SSM Parameter Store as a SecureString (terraform/agentcore_memory.tf's aws_ssm_parameter, encrypted with the no-cost AWS-managed alias/aws/ssm key, not a customer-managed KMS key), never in the agent's environment variables in plaintext, and never committed to this repo -- pass it via TF_VAR_tmdb_api_key or terraform.tfvars (gitignored). Left empty by default since it's optional until the Boredom Buster agent actually receives a request that needs it; a real request will fail cleanly if this is empty."
  type        = string
  default     = ""
  sensitive   = true
}
