# ============================================================
# S3 bucket for the task registry (task_registry.json) -- see
# solution-design.md Section 11 for the hot-reload extensibility
# design this supports.
#
# Bucket names must be globally unique across all of AWS, so a
# random suffix is appended rather than relying on project_name
# alone.
# ============================================================

resource "random_id" "task_registry_suffix" {
  byte_length = 4
}

locals {
  task_registry_key = "task_registry.json"
}

resource "aws_s3_bucket" "task_registry" {
  bucket = "${var.project_name}-task-registry-${var.environment}-${random_id.task_registry_suffix.hex}"
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "task_registry" {
  bucket = aws_s3_bucket.task_registry.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "task_registry" {
  bucket = aws_s3_bucket.task_registry.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "task_registry" {
  bucket                  = aws_s3_bucket.task_registry.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The task registry is templated (task_registry.json.tpl) rather than
# a static file, because GetWeather's entry now needs a real value
# Terraform only knows at apply time: the get_weather_task Lambda's
# ARN (terraform/lambda_tasks.tf). This is a genuine improvement over
# the old placeholder-ARN approach for AgentCore-backed tasks (which
# still need a manual paste after `agentcore deploy`, since that ARN
# doesn't exist until a separate, external deploy step runs) -- for a
# same-account Lambda function, Terraform creates it and knows its
# ARN in the same apply, so there's no reason to hand-edit it in.
#
# Uses the built-in templatefile() function rather than the deprecated/
# archived `hashicorp/template` provider's template_file data source --
# no extra provider dependency needed for a single string substitution.
locals {
  task_registry_rendered = templatefile("${path.module}/../task_registry/task_registry.json.tpl", {
    get_weather_task_lambda_arn      = aws_lambda_function.get_weather_task.arn
    boredom_buster_agent_runtime_arn = aws_bedrockagentcore_agent_runtime.boredom_buster.agent_runtime_arn
  })
}

# Uploads the rendered task registry as the initial object.
# Subsequent updates to add new tasks can either re-run terraform
# apply (this resource tracks the rendered content's hash and will
# update in place) or be pushed directly via `aws s3 cp` for a true
# hot-reload without any Terraform involvement at all -- both paths
# are valid per solution-design.md Section 11.
resource "aws_s3_object" "task_registry" {
  bucket  = aws_s3_bucket.task_registry.id
  key     = local.task_registry_key
  content = local.task_registry_rendered
  etag    = md5(local.task_registry_rendered)

  tags = var.tags
}
