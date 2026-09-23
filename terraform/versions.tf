terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # v6.0+ required: aws_bedrockagentcore_memory and
      # aws_bedrockagentcore_memory_strategy (Boredom Buster's long-term
      # memory resources, terraform/agentcore_memory.tf) are only
      # available from this version onward. v6's other headline change
      # (optional per-resource region overrides) isn't used by this
      # single-region project.
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
    local = {
      # Used by agentcore_runtime.tf's data "local_file" to read the
      # agent package .zip built by scripts/build_agentcore_package.py
      # (a null_resource + local-exec step, since building it requires
      # running pip -- something the archive_file data source, used
      # for the plain-Python Lambda functions elsewhere in this
      # project, can't do).
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
