# ============================================================
# Alexa skill deployment via SMAPI, invoked from Terraform.
#
# There is no native Terraform resource for an Alexa skill -- the
# AWS provider has no aws_alexa_* resources.
#
# Instead, scripts/deploy_skill.py is invoked here via a
# null_resource + local-exec, driving the ASK Skill Management API
# (SMAPI) directly over HTTPS. Credentials for SMAPI (LWA client ID/
# secret/refresh token, vendor ID) are read from environment
# variables by the script itself -- they are deliberately NOT
# Terraform variables, so they never end up in .tf files or in
# tfstate. Export them in your shell (or CI secrets) before running
# terraform apply:
#   ASK_LWA_CLIENT_ID, ASK_LWA_CLIENT_SECRET, ASK_LWA_REFRESH_TOKEN,
#   ASK_VENDOR_ID
#
# DEPLOYMENT ORDER NOTE: on a brand-new deployment, alexa_skill_id
# is empty. SMAPI refuses to import a skill manifest pointing at a
# Lambda that has NO Alexa-invoke permission at all (confirmed
# against Amazon's docs: "You must configure at least one trigger
# for your function to grant Alexa the necessary invocation
# permissions") -- without one, this null_resource's SMAPI import
# fails with "The trigger setting for the Lambda ... is invalid."
# See lambda.tf's aws_lambda_permission.alexa_invoke_bootstrap /
# alexa_invoke_scoped for the two-permission bootstrap pattern this
# depends on. The recommended sequence is:
#   1. terraform apply                 (creates the Lambda, creates
#                                        the unscoped bootstrap
#                                        permission since
#                                        alexa_skill_id is still
#                                        empty, then runs this
#                                        script to CREATE the skill
#                                        -- the bootstrap permission
#                                        now exists so SMAPI accepts
#                                        the import -- and prints the
#                                        new skill ID)
#   2. Set alexa_skill_id in
#      terraform.tfvars to the printed
#      value
#   3. terraform apply again           (destroys the bootstrap
#                                        permission, creates the
#                                        scoped permission now that
#                                        the skill ID is known, then
#                                        re-runs this script with
#                                        --skill-id set, which
#                                        UPDATES the existing skill
#                                        instead of creating a new
#                                        one)
# This two-apply sequence is a direct consequence of the skill ID
# only existing after the SMAPI create call completes -- it cannot
# be avoided by rearranging resources, since Terraform cannot know
# a value that doesn't exist until an external API call finishes.
# ============================================================

resource "null_resource" "deploy_skill" {
  triggers = {
    lambda_arn             = aws_lambda_function.alexa_skill.arn
    skill_json_hash        = filemd5("${path.module}/../skill_package/skill.json")
    interaction_model_hash = filemd5("${path.module}/../skill_package/interactionModels/custom/en-US.json")
    alexa_skill_id         = var.alexa_skill_id
    small_icon_object_etag = aws_s3_object.skill_icon_small.etag
    large_icon_object_etag = aws_s3_object.skill_icon_large.etag
  }

  provisioner "local-exec" {
    command = <<-EOT
      python3 "${path.module}/../scripts/deploy_skill.py" \
        --skill-package-dir "${path.module}/../skill_package" \
        --lambda-arn "${aws_lambda_function.alexa_skill.arn}" \
        --skill-id "${var.alexa_skill_id}" \
        --small-icon-uri "${local.small_icon_uri}" \
        --large-icon-uri "${local.large_icon_uri}"
    EOT
  }

  # Depends on BOTH permission resources (not just the Lambda
  # function itself) so Terraform always creates/updates whichever
  # permission is active BEFORE running the SMAPI import script --
  # otherwise Terraform's dependency graph has no reason to order
  # the permission grant before this provisioner, and the two could
  # run in parallel, hitting the exact "trigger setting is invalid"
  # error this dependency exists to prevent. depends_on referencing
  # a count=0 resource is valid in Terraform -- it simply resolves
  # to zero instances and is a no-op when that resource isn't
  # created this apply.
  #
  # Also depends on both icon objects existing in S3 (icons.tf) --
  # the manifest's smallIconUri/largeIconUri must be reachable at
  # import time, or Alexa's own console/app rendering may show a
  # broken image until the next successful fetch.
  depends_on = [
    aws_lambda_function.alexa_skill,
    aws_lambda_permission.alexa_invoke_bootstrap,
    aws_lambda_permission.alexa_invoke_scoped,
    aws_s3_object.skill_icon_small,
    aws_s3_object.skill_icon_large,
  ]
}
