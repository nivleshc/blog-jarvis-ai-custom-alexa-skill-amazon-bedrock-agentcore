# ============================================================
# Bedrock AgentCore Memory -- Boredom Buster's long-term memory,
# per solution-design.md Section 12.3.
#
# Uses the AWS provider's native aws_bedrockagentcore_memory /
# aws_bedrockagentcore_memory_strategy resources (available from
# provider v6.0+, see versions.tf's comment on the version bump --
# confirmed directly against the provider's registry docs before
# using them, not assumed to exist). The Runtime agent itself
# (agents/boredom_buster_agent/) is ALSO fully Terraform-managed, via
# terraform/agentcore_runtime.tf's aws_bedrockagentcore_agent_runtime --
# no external `agentcore deploy` CLI step for either resource.
#
# Two long-term strategies, matching solution-design.md Section 12.3
# exactly:
#   - SEMANTIC: liked/disliked titles and inferred genre/mood facts,
#     namespaced per actor (Alexa userId) so one user's preferences
#     never leak into another's recommendations.
#   - SUMMARIZATION: a rolling summary of what's been discussed in a
#     session, namespaced per actor+session, supporting short-term
#     conversational continuity within a single Boredom Buster
#     conversation (e.g. "something funnier" after an initial
#     recommendation, without repeating the whole request).
# USER_PREFERENCE is deliberately NOT used as a separate strategy --
# SEMANTIC already covers "facts about what this user likes," and
# adding USER_PREFERENCE on top would mean two strategies extracting
# overlapping information from the same conversations for no benefit,
# given AgentCore Memory's 6-strategies-per-memory cap.
# ============================================================

data "aws_iam_policy_document" "boredom_buster_memory_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

# Execution role AgentCore Memory assumes to run the model-based
# extraction/consolidation behind the SEMANTIC and SUMMARIZATION
# strategies below (both are built-in strategy types, which invoke a
# Bedrock model on the service's behalf -- confirmed against AWS's own
# aws_bedrockagentcore_memory_strategy docs and the AWS-managed policy
# name used below, not assumed).
resource "aws_iam_role" "boredom_buster_memory_exec" {
  name               = "${var.project_name}-boredom-buster-memory-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.boredom_buster_memory_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "boredom_buster_memory_model_inference" {
  role       = aws_iam_role.boredom_buster_memory_exec.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy"
}

resource "aws_bedrockagentcore_memory" "boredom_buster" {
  name        = "${replace(var.project_name, "-", "_")}_boredom_buster_${var.environment}"
  description = "Boredom Buster's short-term (session) and long-term (per-user preference) memory -- solution-design.md Section 12.3"

  # 90 days: long enough that a user's preferences remain useful across
  # realistic gaps between uses (weeks, not days), short enough that
  # this doesn't become an unbounded, ever-growing store. Within AWS's
  # allowed 7-365 day range for event_expiry_duration.
  event_expiry_duration = 90

  memory_execution_role_arn = aws_iam_role.boredom_buster_memory_exec.arn

  tags = merge(var.tags, {
    Purpose = "Boredom Buster AgentCore agent memory - short-term session + long-term preferences"
  })
}

resource "aws_bedrockagentcore_memory_strategy" "boredom_buster_preferences" {
  name        = "BoredomBusterPreferences"
  memory_id   = aws_bedrockagentcore_memory.boredom_buster.id
  type        = "SEMANTIC"
  description = "Extracts liked/disliked titles and inferred genre/mood preferences, namespaced per Alexa userId"

  # {actorId} is the Alexa userId (see agents/boredom_buster_agent's
  # README for how actor_id is set to session.user.userId on every
  # invocation) -- this is what makes preferences genuinely per-user
  # and persistent across sessions, not just within one conversation.
  namespaces = ["/preferences/{actorId}/"]
}

resource "aws_bedrockagentcore_memory_strategy" "boredom_buster_session_summary" {
  name        = "BoredomBusterSessionSummary"
  memory_id   = aws_bedrockagentcore_memory.boredom_buster.id
  type        = "SUMMARIZATION"
  description = "Rolling summary of the current conversation, namespaced per actor+session for short-term continuity"

  namespaces = ["/summaries/{actorId}/{sessionId}/"]

  # Explicit depends_on to force serialization with the preferences
  # strategy above. Both resources are backed by the same UpdateMemory
  # API call under the hood (a strategy isn't a standalone resource --
  # it's a mutation of the parent Memory resource). Without this,
  # Terraform has no implicit dependency between the two sibling
  # aws_bedrockagentcore_memory_strategy resources (neither references
  # the other, only the shared memory_id), so it creates them in
  # parallel by default -- the second UpdateMemory call then lands
  # while the memory is still in the transitional UPDATING state from
  # the first, and fails with exactly the ValidationException above.
  depends_on = [aws_bedrockagentcore_memory_strategy.boredom_buster_preferences]
}

# ------------------------------------------------------------
# TMDB API key -- SSM Parameter Store (SecureString), not a
# plaintext environment variable, per this project's existing
# pattern of never putting secrets directly in Terraform
# variables/state or Lambda env vars (see DEPLOYMENT.md's
# credential-handling sections for the same principle applied to
# LWA/SMAPI credentials). Deliberately Parameter Store over Secrets
# Manager: this project has no need for automatic secret rotation,
# and Secrets Manager charges ~$0.40/secret/month plus API-call
# charges, while a Parameter Store standard-tier SecureString
# parameter is free (encrypted with the no-cost AWS-managed
# `alias/aws/ssm` key -- no customer-managed KMS key is created
# here, since a CMK carries its own $1/month charge). This project
# exists partly to be cheap enough that readers actually deploy it,
# so free-tier-equivalent services are preferred wherever they serve
# the same purpose -- see solution-design.md's cost-consciousness
# throughout. The Boredom Buster AgentCore Runtime agent (terraform/
# agentcore_runtime.tf) reads this parameter's NAME from its
# TMDB_API_KEY_PARAMETER_NAME environment variable and fetches the
# value itself via ssm:GetParameter at request time -- see that file's
# IAM policy grant and agents/boredom_buster_agent/tmdb_client.py's
# _get_api_key().
# ------------------------------------------------------------
resource "aws_ssm_parameter" "tmdb_api_key" {
  name        = "/${var.project_name}/${var.environment}/tmdb-api-key"
  description = "TMDB (themoviedb.org) API key for Boredom Buster's search_movies/search_tv_shows tools"
  type        = "SecureString"
  # No key_id set -- uses the no-cost AWS-managed alias/aws/ssm key,
  # not a customer-managed KMS key.
  value = var.tmdb_api_key
  tags  = var.tags
}
