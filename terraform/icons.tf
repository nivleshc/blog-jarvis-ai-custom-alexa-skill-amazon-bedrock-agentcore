# ============================================================
# S3 bucket for the skill's icon assets (smallIconUri/largeIconUri in
# skill_package/skill.json's manifest).
#
# WHY A SEPARATE, PUBLIC BUCKET: Alexa's skill manifest requires
# smallIconUri/largeIconUri to be publicly reachable HTTPS URLs --
# confirmed against Amazon's own "Define Skill Store Details for
# Publication" documentation, which shows these as plain public image
# URLs (e.g. m.media-amazon.com/...), not signed/authenticated links.
# This is deliberately a DIFFERENT bucket from task_registry (s3.tf),
# which stays fully private -- the task registry contains no
# information that needs to be public, while the icons must be
# public for Alexa's own UI (developer console, Alexa app skill
# cards, Echo Show display) to render them at all.
#
# Public access is scoped as narrowly as S3 allows: bucket policy
# grants s3:GetObject only, only on the icons/* prefix, to anyone
# (required, since Alexa's own servers/CDN fetch these with no
# credentials) -- not full bucket read, not write, not listing.
# ============================================================

resource "random_id" "skill_icons_suffix" {
  byte_length = 4
}

locals {
  small_icon_key = "icons/skill-icon-small-108.png"
  large_icon_key = "icons/skill-icon-large-512.png"
}

resource "aws_s3_bucket" "skill_icons" {
  bucket = "${var.project_name}-skill-icons-${var.environment}-${random_id.skill_icons_suffix.hex}"
  tags   = var.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "skill_icons" {
  bucket = aws_s3_bucket.skill_icons.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Public access block is intentionally NOT fully locked down here
# (unlike s3.tf's task_registry bucket) -- block_public_policy and
# restrict_public_buckets must be false for the bucket policy below
# (granting public GetObject) to take effect. ACLs remain blocked
# either way since this project only ever uses bucket policies, never
# object ACLs, to grant access.
resource "aws_s3_bucket_public_access_block" "skill_icons" {
  bucket                  = aws_s3_bucket.skill_icons.id
  block_public_acls       = true
  block_public_policy     = false
  ignore_public_acls      = true
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "skill_icons_public_read" {
  bucket = aws_s3_bucket.skill_icons.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadIconsOnly"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.skill_icons.arn}/icons/*"
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.skill_icons]
}

resource "aws_s3_object" "skill_icon_small" {
  bucket       = aws_s3_bucket.skill_icons.id
  key          = local.small_icon_key
  source       = "${path.module}/../assets/icons/skill-icon-small-108.png"
  etag         = filemd5("${path.module}/../assets/icons/skill-icon-small-108.png")
  content_type = "image/png"
  tags         = var.tags
}

resource "aws_s3_object" "skill_icon_large" {
  bucket       = aws_s3_bucket.skill_icons.id
  key          = local.large_icon_key
  source       = "${path.module}/../assets/icons/skill-icon-large-512.png"
  etag         = filemd5("${path.module}/../assets/icons/skill-icon-large-512.png")
  content_type = "image/png"
  tags         = var.tags
}

locals {
  small_icon_uri = "https://${aws_s3_bucket.skill_icons.bucket_regional_domain_name}/${local.small_icon_key}"
  large_icon_uri = "https://${aws_s3_bucket.skill_icons.bucket_regional_domain_name}/${local.large_icon_key}"
}
