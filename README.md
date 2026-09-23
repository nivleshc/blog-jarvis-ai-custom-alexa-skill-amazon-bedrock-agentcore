# Jarvis AI - A Custom Alexa Skill Powered by Amazon Bedrock and AgentCore

Jarvis AI is a custom Alexa skill that runs on any Alexa-enabled device, including older Echo hardware that Alexa+ will never support. It answers general questions through Amazon Bedrock (Amazon Nova Lite), checks the weather through a plain AWS Lambda function, and recommends movies and TV shows through a real Bedrock AgentCore agent with its own persistent memory, all deployed entirely through Terraform into your own AWS account.

This README is a standalone runbook for deploying the solution. It does not assume you have read anything else in this repository.

## What gets deployed

- An AWS Lambda function running the full Alexa request pipeline (skill authentication, a per-user allowlist, rate limiting, and routing)
- Four DynamoDB tables: a user allowlist, two rate-limit counter tables, and a permanent call log
- A Bedrock AgentCore Runtime agent ("Boredom Buster") with its own persistent memory, deployed via direct code deployment, no containers or CLI needed
- A plain Lambda function for weather lookups
- S3 buckets for the task registry, the AgentCore deployment package, and the skill icons
- A CloudWatch dashboard, three alarms, an SNS topic, and an AWS Budget
- The Alexa skill itself (manifest, interaction model, and Lambda permissions), created and updated via the Alexa Skill Management API (SMAPI)

A single `terraform apply` (run twice, see Step 8) creates all of it.

## Before you start: two separate sets of credentials

This deployment needs two completely separate sets of credentials, used for two different purposes:

| | AWS credentials | Alexa/SMAPI credentials |
|---|---|---|
| Used by | Terraform, to create AWS resources | A Python script (invoked automatically by Terraform) that creates the Alexa skill itself |
| Authenticates against | Your AWS account | Your Amazon **developer** account |
| Obtained in | Step 2 | Steps 4-5 |

Both are required for a single `terraform apply` to succeed.

## Step 1: Install the required tools

| Tool | Version | Check with |
|---|---|---|
| [Terraform](https://developer.hashicorp.com/terraform/install) | >= 1.6.0 | `terraform version` |
| Python | 3.12 | `python3 --version` |
| [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | v2 | `aws --version` |
| [Node.js](https://nodejs.org/) | 18 or later | `node --version` |
| ASK CLI | v2.x | `ask --version` |

Node.js is only needed to install the ASK CLI, which in turn is only needed once, to generate an Alexa refresh token in Step 5. Everything else in this project is Python.

Install the ASK CLI:

```bash
npm install -g ask-cli
```

## Step 2: Configure your AWS credentials

Configure AWS credentials in the shell you'll deploy from, either with:

```bash
aws configure
```

or by exporting `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`. Confirm they work with:

```bash
aws sts get-caller-identity
```

Decide your AWS region. This project defaults to `us-east-1`, since that's where Amazon Bedrock and Bedrock AgentCore have the widest model availability. If you deploy elsewhere, confirm Amazon Nova Lite and Bedrock AgentCore Runtime are both available in that region first.

Your AWS account needs permissions to create IAM roles/policies, Lambda functions, DynamoDB tables, S3 buckets, CloudWatch log groups/dashboards/alarms, SNS topics, and AWS Budgets. Administrator access is the simplest option for a personal or sandbox account.

## Step 3: Create an Amazon developer account and get your vendor ID

Your AWS account and your Amazon developer account are two different things. The Alexa skill belongs to the developer account, not AWS.

1. If you don't already have one, create a free account at [developer.amazon.com](https://developer.amazon.com).
2. Log in and get your vendor ID from [developer.amazon.com/settings/console/mycid](https://developer.amazon.com/settings/console/mycid). You'll use this as `ASK_VENDOR_ID` in Step 6.

## Step 4: Create a Login with Amazon (LWA) security profile

SMAPI (the Alexa Skill Management API) only accepts security profiles created through the dedicated [LWA console](https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html), not the general login.amazon.com sign-in page. Use the link below.

1. Go to the [LWA console](https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html) and log in with your Amazon developer account.
2. If you don't already have a security profile, click **Create a New Security Profile** and fill in the required fields.
3. Find your profile in the list, click the gear icon, choose **Web Settings**, then click **Edit**.
4. Under **Allowed Return URLs** (not Allowed Origins), add both of these, then click **Save**:
   ```
   http://127.0.0.1:9090/cb
   https://ask-cli-static-content.s3-us-west-2.amazonaws.com/html/ask-cli-no-browser.html
   ```
5. Back in **Web Settings**, copy the **Client ID** and **Client Secret**. You'll use these as `ASK_LWA_CLIENT_ID` and `ASK_LWA_CLIENT_SECRET`.

## Step 5: Generate your SMAPI refresh token

Using the ASK CLI you installed in Step 1, generate a refresh token, passing the client ID and secret from Step 4 explicitly:

```bash
ask util generate-lwa-tokens \
  --client-id <your-client-id> \
  --client-confirmation <your-client-secret>
```

This opens a browser window, asks you to log in and click **Allow**. Once you have done that, return to the CLI where you will be asked "Do you confirm that you used the browser to sign in to Alexa Skills Kit Tools?". Type Yes and press enter. It should then print a refresh token in your terminal. Use this as `ASK_LWA_REFRESH_TOKEN`.

**If you have more than one LWA security profile**, always pass `--client-id`/`--client-confirmation` explicitly as shown above. Running the command with no arguments silently falls back to whatever profile is the ASK CLI's own default, which may not be the one you just created.

**Important**: `ASK_LWA_CLIENT_ID`, `ASK_LWA_CLIENT_SECRET`, and `ASK_LWA_REFRESH_TOKEN` are a matched set. If you ever regenerate the client secret on the LWA console, every refresh token issued under the old secret stops working immediately. If deployment later fails with `unauthorized_client`, regenerate the refresh token fresh from the security profile's current secret and re-export all three values together.

You should now have four values: `ASK_LWA_CLIENT_ID`, `ASK_LWA_CLIENT_SECRET`, `ASK_LWA_REFRESH_TOKEN`, and `ASK_VENDOR_ID` (from Step 3). Keep them somewhere safe.

## Step 6: Check Bedrock model access

1. Open the [Bedrock console](https://console.aws.amazon.com/bedrock/) in the same region you chose in Step 2.
2. In the left navigation, under **Bedrock configurations**, choose **Model access**.
3. Confirm **Amazon Nova Lite** shows as accessible. If it doesn't, request/enable it from the same page. For Amazon-owned models this is normally immediate.

## Step 7: Get a free TMDB API key

The movie/TV recommendation agent needs a free API key from The Movie Database to search for titles.

1. Create a free account at [themoviedb.org](https://www.themoviedb.org/) if you don't already have one.
2. Go to [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) and request a free Developer API key. Approval for personal, non-commercial use is normally immediate.

## Step 8: Set your environment variables

Export the following in the shell you'll run `terraform apply` from:

```bash
export ASK_LWA_CLIENT_ID="<from Step 4>"
export ASK_LWA_CLIENT_SECRET="<from Step 4>"
export ASK_LWA_REFRESH_TOKEN="<from Step 5>"
export ASK_VENDOR_ID="<from Step 3>"
export TF_VAR_tmdb_api_key="<from Step 7>"
```

These are deliberately environment variables, not values stored in a `.tf` file. The deployment script reads the first four directly from the environment so they never end up in Terraform state.

## Step 9: Configure Terraform

We will use an Amazon S3 bucket to store the Terraform state file and for session locks. Use the instructions in this file https://developer.hashicorp.com/terraform/language/backend/s3 to create an Amazon S3 bucket.

Once created, open the file `terraform/backend.tf` and add the bucket, key and region details.
Below is an example of what is expected.
```hcl
terraform {
  backend "s3" {
    bucket = "myterraformbucket"
    key    = "terraform/jarvis-ai/terraform.tfstate"
    region = "ap-southeast-2"

    encrypt      = true
    use_lockfile = true
  }
}
```

Open `terraform/terraform.tfvars`and uncomment the line shown below, and replace you@example.com with your own email address:

```hcl
alert_email = "you@example.com"   # required, no default
```

That's the only value you're required to set, everything else has a working default in `terraform/variables.tf`. Leave `alexa_skill_id` unset for now, it starts empty on purpose (see Step 11).

If you want to change any of the defaults, add the relevant line(s) below to the same `terraform.tfvars` file. Nothing here is required, this table exists so you know exactly what you're accepting by leaving a value unset.

| Variable | Default | What it controls |
|---|---|---|
| `aws_region` | `us-east-1` | AWS region everything deploys into |
| `environment` | `dev` | Environment name, used in resource naming/tagging |
| `bedrock_model_id` | `amazon.nova-lite-v1:0` | The Bedrock model used for general Q&A. Must be an Amazon-owned model |
| `bedrock_max_tokens` | `512` | Hard output token cap on every Bedrock/AgentCore call |
| `max_input_chars` | `800` | Hard input character cap, checked before any Bedrock/AgentCore call is made |
| `input_price_per_million_tokens` | `0.06` | Nova Lite input price per 1M tokens, used for the cost calculations in `SkillCallLog`. Re-check against the [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) periodically |
| `output_price_per_million_tokens` | `0.24` | Nova Lite output price per 1M tokens, same re-check note as above |
| `global_default_daily_limit` | `20` | Default per-user daily request limit |
| `global_default_burst_limit` | `20` | Default per-user hourly burst limit |
| `global_daily_ceiling` | `200` | Aggregate cap on total requests across every approved user combined, per day |
| `usage_counter_ttl_days` | `2` | How long rate-limit counter rows live before auto-expiring |
| `call_log_ttl_days` | `0` | How long `SkillCallLog` rows live before auto-expiring. `0` keeps history forever |
| `daily_cost_alarm_threshold_usd` | `5` | CloudWatch alarm threshold for estimated daily cost |
| `denial_rate_alarm_threshold` | `50` | CloudWatch alarm threshold for denied requests in a 5 minute window |
| `lambda_error_rate_alarm_threshold` | `5` | CloudWatch alarm threshold for Lambda errors in a 5 minute window |
| `monthly_budget_usd` | `25` | AWS Budget monthly limit in USD, the last-resort billing tripwire |
| `tmdb_watch_region` | `AU` | Two-letter ISO-3166-1 country code used for the "where to watch" lookups on Boredom Buster's detail screen. Set this to your own country |
| `lambda_timeout` | `30` | Lambda timeout in seconds |
| `lambda_memory_size` | `256` | Lambda memory in MB |
| `lambda_reserved_concurrency` | `10` | Caps how many Lambda invocations can run in parallel across all users combined |
| `cloudwatch_log_retention_days` | `30` | CloudWatch Logs retention for the Lambda's log group |

`alert_email` (Step 9, above) and `tmdb_api_key` (set via `TF_VAR_tmdb_api_key` in Step 8, not in `terraform.tfvars`, since it's a secret) are the only two values with no usable default.

A worked example `terraform.tfvars`, overriding a couple of the defaults:

```hcl
alert_email       = "you@example.com"
aws_region        = "us-east-1"
tmdb_watch_region = "US"
monthly_budget_usd = 15
```

## Step 10: First Terraform apply

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

Review the plan, then confirm. This creates every AWS resource (DynamoDB tables, Lambda function, S3 buckets, CloudWatch dashboard/alarms, SNS topic, AWS Budget, and the Boredom Buster AgentCore agent), then runs a script against SMAPI that creates a brand-new Alexa skill. It prints the new skill's ID to your terminal as JSON:

```
{"skillId": "amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
```
**Note**  
The skillId is not printed at the end when Terraform has finished deploying everything, but well before that, when the null resource tries to create the skill.
Look for lines similar to what is shown below to find the skillId
```
null_resource.deploy_skill (local-exec): Import status: IN_PROGRESS
null_resource.deploy_skill (local-exec): Import status: SUCCEEDED
null_resource.deploy_skill (local-exec): {"skillId": "amzn1.ask.skill.a1234567-abcd-4199-a111-a12345678901"}
null_resource.deploy_skill: Still creating... [1m0s elapsed]
null_resource.deploy_skill: Creation complete after 1m1s [id=123456789012345678]
```

Copy that value, you'll need it in the next step.

## Step 11: Second Terraform apply

Inside `terraform.tfvars` uncomment the line shown below, and update the skill ID with the value you got from Step 10:

```hcl
alexa_skill_id = "amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Then run:

```bash
terraform apply
```

This is not an extra step by mistake, it's required. Terraform has no way of knowing the skill ID until SMAPI hands one back on the first apply, so the first apply creates the skill using a temporary, unscoped Lambda permission just so SMAPI has something to import against. This second apply replaces that temporary permission with one properly scoped to your exact skill ID.

**Note**
If you get the following error, rerun `terraform apply` after a few minutes. The error description states the exact issue, the Amazon Bedrock AgentCore Memory is currently in "Updating" state and cannot be updated. You can monitor this from the AWS Management Console and once the Memory status has changed from "Updating" to "Active", rerun `terraform apply`.
```
│ Error: creating Bedrock AgentCore Memory Strategy
│ 
│   with aws_bedrockagentcore_memory_strategy.boredom_buster_session_summary,
│   on agentcore_memory.tf line 89, in resource "aws_bedrockagentcore_memory_strategy" "boredom_buster_session_summary":
│   89: resource "aws_bedrockagentcore_memory_strategy" "boredom_buster_session_summary" {
│ 
│ Cause: operation error Bedrock AgentCore Control: UpdateMemory, , ValidationException: Validation failed during UpdateMemory: Memory is in
│ transitional state UPDATING. Cannot update memory."
```

Once this apply finishes, every AWS resource and the Alexa skill itself both exist and are correctly linked together.

## Step 12: Confirm the SNS email subscription

Check the inbox for the address you set as `alert_email` in Step 9, for an email from AWS Notifications with a **Confirm subscription** link, and click it. Until you confirm it, the CloudWatch alarms and AWS Budget alerts have nowhere to deliver to.

## Step 13: Activate the cost allocation tag (optional but recommended)

1. Open the [Cost Allocation Tags page](https://console.aws.amazon.com/billing/home#/tags) in the AWS Billing console.
2. Under **User-Defined Cost Allocation Tags**, find `Project`, select it, and click **Activate**.

This can take up to 24 hours to propagate. It only affects cost breakdown by tag in Cost Explorer, not the actual budget alerts, which work regardless.

## Step 14: Enable the skill for testing

SMAPI's import in Step 10 uploaded the skill manifest and interaction model, but it does not automatically enable the skill for testing.

1. Go to the [Alexa developer console](https://developer.amazon.com/alexa/console/ask) and open your skill ("Jarvis AI").
2. Go to the **Test** tab.
3. Where it says "Test is disabled for this skill", change the dropdown from **Off** to **Development**.

## Step 15: Approve your own test account

Even as the developer, you will not get real answers from the skill until your own Alexa user ID is approved. This is deny-by-default access control working as intended, not a bug.  

**Sidenote**: You can ask Alexa to open the "Jarvis AI" skill. It will happily load it and welcome you to the skill, asking you what you would like to do. However, as soon as you say or press any of the cards, Alexa will respond with "You are not authorised to use this skill yet. The developer has been notified and will review your request".

1. Ask the skill a question, through the Alexa app, the developer console's test simulator, or a real device signed into the same developer account. This will be denied, which is expected, but it creates a `pending` record with your real Alexa user ID.
2. Find and approve yourself:
   ```bash
   # run the following from the root of the repository

   export SKILL_USERS_TABLE=$(terraform -chdir=terraform output -raw skill_users_table_name)
   python3 scripts/approve_user.py --region us-east-1 --list-pending
   python3 scripts/approve_user.py --region us-east-1 --approve <userId>
   ```
   (Use the region you actually deployed into if it isn't `us-east-1`.)
3. Ask the skill a question again. You should now get a real answer, routed through Amazon Nova Lite.

## Step 16: Verify the deployment

- Open the CloudWatch dashboard: `terraform -chdir=terraform output cloudwatch_dashboard_name`, then find it under Dashboards in the CloudWatch console. After a couple of test calls, the "Daily Estimated Cost" and "Requests by Outcome" panels should show data.
- Run a usage report:
  ```bash
  export SKILL_CALL_LOG_TABLE=$(terraform -chdir=terraform output -raw skill_call_log_table_name)
  python3 scripts/usage_report.py --region us-east-1 --by-day --by-user
  ```

At this point Jarvis AI is fully deployed and working: general questions go to Amazon Nova Lite, weather questions go to a plain Lambda function, and movie/TV recommendation requests go to the Boredom Buster AgentCore agent with its own persistent memory.

## Tearing it down

```bash
cd terraform
terraform destroy
```

This removes every AWS resource this project created (Lambda, DynamoDB tables, S3 buckets, IAM roles, CloudWatch resources, SNS topic, AWS Budget). It does **not** remove the Alexa skill itself, since there's no Terraform resource that represents that action. To delete the skill entirely, do it manually through the [Alexa developer console](https://developer.amazon.com/alexa/console/ask) (select the skill, then the "..." menu, then Delete).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| SMAPI fails with `unauthorized_client` | The refresh token doesn't match the current client ID/secret pair | Re-run `ask util generate-lwa-tokens` with `--client-id`/`--client-confirmation` and re-export all three `ASK_LWA_*` values from that one run |
| SMAPI fails with `invalid_client` | `ASK_LWA_CLIENT_ID` or `ASK_LWA_CLIENT_SECRET` is wrong | Re-copy both from the LWA console's Web Settings |
| SMAPI fails with `invalid_grant` | `ASK_LWA_REFRESH_TOKEN` is stale | Re-run `ask util generate-lwa-tokens` for a fresh token |
| `approve_user.py`/`usage_report.py` fails with `ResourceNotFoundException` | Region mismatch between your AWS CLI profile's default region and where you deployed | Pass `--region` explicitly matching your deployment region |
| Skill test simulator times out or can't reach the skill | Skill not enabled for testing | Repeat Step 14 |
| Every request gets "you don't have access yet" | Expected until you approve your own user ID | Repeat Step 15 |
| No CloudWatch alarm emails arrive | SNS subscription not confirmed | Repeat Step 12 |
| `AccessDeniedException` on the first real question | Bedrock model access not enabled in your region | Repeat Step 6 |
