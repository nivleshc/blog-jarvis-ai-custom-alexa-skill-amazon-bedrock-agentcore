# ============================================================
# DynamoDB tables -- see solution-design.md Section 5 for the full
# data model and Section 6 for how the rate-limit tables are used.
#
# All tables use on-demand (PAY_PER_REQUEST) billing -- no capacity
# planning needed at this project's scale, and cost is purely
# per-request, which keeps it aligned with the "deny cheaply before
# spending on Bedrock" design principle.
# ============================================================

# ------------------------------------------------------------
# SkillUsers -- the allowlist. No TTL: this is a permanent record
# of every user who has ever contacted the skill, their approval
# status, their per-user limit overrides, and running usage totals.
# See solution-design.md Section 5.1.
# ------------------------------------------------------------
resource "aws_dynamodb_table" "skill_users" {
  name         = "${var.project_name}-skill-users-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "userId"

  attribute {
    name = "userId"
    type = "S"
  }

  # point_in_time_recovery protects this table specifically because it
  # is the access-control allowlist itself -- losing it would mean
  # losing the record of who is approved.
  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Purpose = "deny-by-default per-user allowlist and running usage totals"
  })
}

# ------------------------------------------------------------
# SkillUsageCounters -- per-user rate limiting (daily + burst/hourly
# buckets, one item per bucket per user). TTL auto-expires old
# buckets so no cleanup job is needed.
# See solution-design.md Section 5.2.
# ------------------------------------------------------------
resource "aws_dynamodb_table" "skill_usage_counters" {
  name         = "${var.project_name}-skill-usage-counters-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "counter_key"

  attribute {
    name = "counter_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = merge(var.tags, {
    Purpose = "per-user daily and burst rate-limit counters"
  })
}

# ------------------------------------------------------------
# GlobalUsageCounters -- aggregate ceiling across ALL approved users
# combined, per day. Independent of any single user's limit -- see
# solution-design.md Section 6 for why this exists in addition to
# per-user limits.
# ------------------------------------------------------------
resource "aws_dynamodb_table" "global_usage_counters" {
  name         = "${var.project_name}-global-usage-counters-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "counter_key"

  attribute {
    name = "counter_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = merge(var.tags, {
    Purpose = "aggregate daily request ceiling across all users"
  })
}

# ------------------------------------------------------------
# SkillCallLog -- per-call audit/cost record. One item per completed
# call (success or failure), with actual token counts from the
# Bedrock/AgentCore response. Sort key on timestamp enables
# per-user time-range queries (e.g. "this user's calls in the last
# 7 days"). TTL is optional and disabled by default -- see
# solution-design.md Section 5.4 and the call_log_ttl_days variable.
# ------------------------------------------------------------
resource "aws_dynamodb_table" "skill_call_log" {
  name         = "${var.project_name}-skill-call-log-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "userId"
  range_key    = "timestamp"

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  dynamic "ttl" {
    for_each = var.call_log_ttl_days > 0 ? [1] : []
    content {
      attribute_name = "ttl"
      enabled        = true
    }
  }

  tags = merge(var.tags, {
    Purpose = "per-call audit log: tokens cost latency outcome"
  })
}

# ------------------------------------------------------------
# SkillWatchlist -- per-user watchlist for movies/TV shows.
# Users can add titles to their watchlist and check availability
# in their region at a glance. Hash key is userId, range key is
# the TMDB ID prefixed with media type (e.g., "movie:550").
# See solution-design.md Section 14 for the full data model.
# ------------------------------------------------------------
resource "aws_dynamodb_table" "skill_watchlist" {
  name         = "${var.project_name}-skill-watchlist-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "userId"
  range_key    = "itemId"

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "itemId"
    type = "S"
  }

  attribute {
    name = "title"
    type = "S"
  }

  # GSI for querying by title (for duplicate checking)
  global_secondary_index {
    name            = "TitleIndex"
    hash_key        = "title"
    projection_type = "ALL"
  }

  tags = merge(var.tags, {
    Purpose = "per-user watchlist with region availability tracking"
  })
}
