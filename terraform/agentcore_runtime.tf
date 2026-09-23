# ============================================================
# Boredom Buster's Bedrock AgentCore Runtime -- fully Terraform-managed,
# via the AWS provider's native aws_bedrockagentcore_agent_runtime
# resource (available from provider v6.0+, same version already
# required for agentcore_memory.tf's Memory resources).
#
# AgentCore Runtime supports "direct code deployment" (a .zip of code +
# dependencies, uploaded to S3, no container/ECR at all), via the AWS
# provider's aws_bedrockagentcore_agent_runtime resource's
# agent_runtime_artifact.code_configuration (see
# registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/
# bedrockagentcore_agent_runtime, "Agent runtime artifact from S3 with
# Code Configuration" example). This avoids an `agentcore` CLI
# dependency and an ECR repository -- the agent deploys in the exact
# same `terraform apply` as everything else this project manages,
# consistent with lambda_tasks.tf's `GetWeather` Lambda function, which
# also deploys with zero external CLI step.
#
# PACKAGING: unlike a plain Lambda function (lambda.tf/lambda_tasks.tf,
# packaged via Terraform's built-in archive_file data source), this
# agent's dependencies (bedrock-agentcore, strands-agents, and their
# own transitive dependencies) must be vendored INTO the deployment
# package as ARM64-compatible wheels -- AgentCore Runtime only
# supports the arm64 instruction set (AWS Graviton), confirmed against
# AWS's own direct-code-deployment troubleshooting docs. archive_file
# only zips existing files; it cannot run pip. Hence
# scripts/build_agentcore_package.py (invoked below via a null_resource
# + local-exec), which runs `pip install --platform
# manylinux2014_aarch64 --only-binary=:all:` to force ARM64 wheels
# regardless of the machine actually running `terraform apply` (Intel,
# AMD, or Apple Silicon all produce the correct output) -- verified
# working end-to-end against this exact agent's requirements.txt
# before wiring it in here (produces a ~28MB package, well under
# AgentCore Runtime's 250MB direct-code-deploy limit).
# ============================================================

resource "random_id" "agent_packages_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "agent_packages" {
  bucket = "${var.project_name}-agent-packages-${var.environment}-${random_id.agent_packages_suffix.hex}"
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "agent_packages" {
  bucket = aws_s3_bucket.agent_packages.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_packages" {
  bucket = aws_s3_bucket.agent_packages.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "agent_packages" {
  bucket                  = aws_s3_bucket.agent_packages.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------
# Build step -- runs scripts/build_agentcore_package.py, which pip-
# installs requirements.txt as ARM64 wheels into a build directory,
# copies the agent's own .py files on top, and zips the result. The
# trigger hash covers every source file AND requirements.txt, so a
# change to either forces a rebuild + re-upload + Runtime update on
# the next apply -- a stale package is never silently left deployed.
# ------------------------------------------------------------
locals {
  boredom_buster_source_dir   = "${path.module}/../agents/boredom_buster_agent"
  boredom_buster_requirements = "${local.boredom_buster_source_dir}/requirements.txt"
  boredom_buster_package_zip  = "${path.module}/agent_build/boredom_buster_agent.zip"

  boredom_buster_source_files = fileset(local.boredom_buster_source_dir, "*.py")
  boredom_buster_source_hash = md5(join("", [
    for f in local.boredom_buster_source_files : filemd5("${local.boredom_buster_source_dir}/${f}")
  ]))
  boredom_buster_requirements_hash = filemd5(local.boredom_buster_requirements)
}

resource "null_resource" "build_boredom_buster_package" {
  triggers = {
    source_hash       = local.boredom_buster_source_hash
    requirements_hash = local.boredom_buster_requirements_hash
  }

  provisioner "local-exec" {
    command = <<-EOT
      python3 "${path.module}/../scripts/build_agentcore_package.py" \
        --source-dir "${local.boredom_buster_source_dir}" \
        --requirements "${local.boredom_buster_requirements}" \
        --output-zip "${local.boredom_buster_package_zip}" \
        --python-version 3.12
    EOT
  }
}

# filemd5() reads the zip AFTER the null_resource above has built it --
# Terraform's dependency graph doesn't automatically know this data
# source depends on that resource (no direct reference between them),
# so depends_on is required here, not optional. Without it, Terraform
# could evaluate this data source before the build ever runs, either
# erroring on a missing file (first apply) or reading a stale zip from
# a previous apply (subsequent applies).
data "local_file" "boredom_buster_package" {
  filename   = local.boredom_buster_package_zip
  depends_on = [null_resource.build_boredom_buster_package]
}

resource "aws_s3_object" "boredom_buster_package" {
  bucket = aws_s3_bucket.agent_packages.id
  key    = "boredom_buster_agent/boredom_buster_agent.zip"
  source = local.boredom_buster_package_zip
  etag   = data.local_file.boredom_buster_package.content_md5

  tags = var.tags

  depends_on = [null_resource.build_boredom_buster_package]
}

# ------------------------------------------------------------
# Execution role -- the role AgentCore Runtime assumes to actually run
# the agent (distinct from agentcore_memory.tf's
# boredom_buster_memory_exec, which is the role AgentCore MEMORY
# assumes for its own model-based extraction -- two different services
# assuming two different roles for two different purposes). Permissions
# below match AWS's own documented execution-role policy for AgentCore
# Runtime (docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
# runtime-permissions.html's "Execution role for running an agent in
# AgentCore Runtime" section) plus this project's own additions
# (Bedrock Memory data-plane actions, the TMDB SSM parameter) -- not a
# broad managed policy, consistent with iam.tf's least-privilege
# approach for the main skill Lambda's role.
# ------------------------------------------------------------

data "aws_iam_policy_document" "boredom_buster_runtime_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "boredom_buster_runtime_exec" {
  name               = "${var.project_name}-bb-runtime-exec-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.boredom_buster_runtime_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "boredom_buster_runtime_logs_and_traces" {
  name = "${var.project_name}-bb-runtime-logs-${var.environment}"
  role = aws_iam_role.boredom_buster_runtime_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CreateLogGroupAndDescribe"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:CreateLogGroup",
        ]
        Resource = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        Sid      = "DescribeAllLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:*"
      },
      {
        Sid    = "WriteOwnLogStreams"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:PutResourcePolicy",
        ]
        Resource = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
      },
      {
        Sid    = "XRayTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ]
        Resource = "*"
      },
      {
        Sid       = "AgentCoreMetrics"
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricData"]
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      }
    ]
  })
}

resource "aws_iam_role_policy" "boredom_buster_runtime_workload_identity" {
  name = "${var.project_name}-bb-runtime-workload-identity-${var.environment}"
  role = aws_iam_role.boredom_buster_runtime_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "GetAgentAccessToken"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        ]
        Resource = [
          "arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${local.account_id}:workload-identity-directory/default",
          "arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${local.account_id}:workload-identity-directory/default/workload-identity/*",
        ]
      }
    ]
  })
}

# Model invocation -- same Nova model the agent's BedrockModel provider
# uses (BEDROCK_MODEL_ID env var below), scoped to the specific model,
# not a wildcard foundation-model resource -- same least-privilege
# principle as iam.tf's lambda_bedrock policy for the main skill Lambda.
resource "aws_iam_role_policy" "boredom_buster_runtime_bedrock_model" {
  name = "${var.project_name}-bb-runtime-bedrock-model-${var.environment}"
  role = aws_iam_role.boredom_buster_runtime_exec.id

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
      }
    ]
  })
}

# AgentCore Memory data-plane access -- memory_client.py's
# create_event()/retrieve_memories() calls, scoped to this specific
# Memory resource only (agentcore_memory.tf's boredom_buster memory),
# not every Memory resource in the account.
resource "aws_iam_role_policy" "boredom_buster_runtime_memory" {
  name = "${var.project_name}-bb-runtime-memory-${var.environment}"
  role = aws_iam_role.boredom_buster_runtime_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MemoryDataPlaneAccess"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:RetrieveMemoryRecords",
          "bedrock-agentcore:ListMemoryRecords",
          "bedrock-agentcore:GetMemoryRecord",
        ]
        Resource = aws_bedrockagentcore_memory.boredom_buster.arn
      }
    ]
  })
}

# TMDB API key -- read-only access to this one SSM parameter, plus
# kms:Decrypt on the AWS-managed alias/aws/ssm key (needed because the
# parameter is a SecureString -- see agentcore_memory.tf's aws_ssm_parameter
# for why Parameter Store, not Secrets Manager, is used here).
resource "aws_iam_role_policy" "boredom_buster_runtime_tmdb_parameter" {
  name = "${var.project_name}-bb-runtime-tmdb-param-${var.environment}"
  role = aws_iam_role.boredom_buster_runtime_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTmdbApiKeyParameter"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.tmdb_api_key.arn
      },
      {
        Sid      = "DecryptSsmDefaultKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:${local.partition}:kms:${var.aws_region}:${local.account_id}:alias/aws/ssm"
      }
    ]
  })
}

# S3 read access to this agent's own deployment package -- required by
# AgentCore Runtime's CreateAgentRuntime/UpdateAgentRuntime API calls
# (confirmed against AWS's own direct-code-deployment troubleshooting
# docs: "The role used to call Create/UpdateAgentRuntime does not have
# s3:GetObject permissions on S3 uri passed in the API input"). Scoped
# to this one bucket, not every bucket in the account.
resource "aws_iam_role_policy" "boredom_buster_runtime_package_read" {
  name = "${var.project_name}-bb-runtime-package-read-${var.environment}"
  role = aws_iam_role.boredom_buster_runtime_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadOwnDeploymentPackage"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.agent_packages.arn}/*"
      }
    ]
  })
}

# ------------------------------------------------------------
# The Runtime itself. code_configuration (not container_configuration)
# means direct code deployment -- no ECR, no Docker build, matching
# this project's Lambda-based tasks' "no external build tooling"
# simplicity as closely as AgentCore Runtime allows.
# ------------------------------------------------------------
resource "aws_bedrockagentcore_agent_runtime" "boredom_buster" {
  agent_runtime_name = "${replace(var.project_name, "-", "_")}_bb_runtime_${var.environment}"
  description        = "Boredom Buster -- movie/TV recommendation agent with multi-tool reasoning and per-user memory. See agents/boredom_buster_agent/README.md and solution-design.md Section 12."
  role_arn           = aws_iam_role.boredom_buster_runtime_exec.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["agent.py"]
      runtime     = "PYTHON_3_12"
      code {
        s3 {
          bucket     = aws_s3_bucket.agent_packages.id
          prefix     = aws_s3_object.boredom_buster_package.key
          version_id = aws_s3_object.boredom_buster_package.version_id
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  environment_variables = {
    BEDROCK_MODEL_ID            = var.bedrock_model_id
    BEDROCK_MAX_TOKENS          = tostring(var.bedrock_max_tokens)
    BEDROCK_AGENTCORE_MEMORY_ID = aws_bedrockagentcore_memory.boredom_buster.id
    # The SSM parameter's NAME, not its value -- tmdb_client.py's
    # _get_api_key() reads this env var and calls ssm:GetParameter at
    # request time. Putting the parameter's value directly here
    # instead would put the secret in plaintext in this resource's
    # environment_variables (visible in Terraform state and the
    # AgentCore console) -- exactly what using SSM Parameter Store was
    # meant to avoid in the first place.
    TMDB_API_KEY_PARAMETER_NAME = aws_ssm_parameter.tmdb_api_key.name
    # Country used for the detail screen's "where to watch" lookup.
    # Streaming rights differ per country, so TMDB's watch-providers
    # endpoint has no global answer -- an explicit region is required.
    # See tmdb_client.get_watch_providers.
    TMDB_WATCH_REGION = var.tmdb_watch_region
  }

  tags = merge(var.tags, {
    Purpose = "Boredom Buster AgentCore Runtime agent"
  })

  depends_on = [
    aws_iam_role_policy.boredom_buster_runtime_logs_and_traces,
    aws_iam_role_policy.boredom_buster_runtime_workload_identity,
    aws_iam_role_policy.boredom_buster_runtime_bedrock_model,
    aws_iam_role_policy.boredom_buster_runtime_memory,
    aws_iam_role_policy.boredom_buster_runtime_tmdb_parameter,
    aws_iam_role_policy.boredom_buster_runtime_package_read,
  ]
}
