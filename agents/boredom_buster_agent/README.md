# Boredom Buster -- Real Bedrock AgentCore Agent

This project's genuine Bedrock AgentCore showcase: a task with enough
real multi-step reasoning, tool selection, and persistent memory to
demonstrate what AgentCore is actually for, unlike `GetWeather`'s
single deterministic API lookup.

## What it does

Recommends movies and TV shows (deliberately no books) based on a
spoken mood/genre request, using genuine multi-step reasoning:

- Asks a clarifying question if the request is too vague to search on.
- Searches TMDB for real candidates (movies and/or TV shows).
- Checks the requesting user's memory for prior likes/dislikes of a
  candidate before recommending it.
- Records like/dislike feedback, which AgentCore Memory's long-term
  SEMANTIC strategy turns into a durable per-user preference fact --
  so recommendations genuinely improve with use.

This is the multi-step reasoning + tool selection + persistent memory
combination that AgentCore Runtime/Memory exist to support, unlike
`GetWeather`'s single deterministic API lookup.

## Framework and model

Built with the [Strands Agents SDK](https://strandsagents.com/)
(`strands-agents`), the framework AWS's own AgentCore documentation
demonstrates throughout its memory-integration guides. Uses Amazon Nova
Lite via Strands' `BedrockModel` provider -- the same model this
project already uses for `AskBedrock`, kept consistent rather than
introducing a second model choice without a specific reason to.

## Files

- `agent.py` -- the agent itself. `@app.entrypoint`-decorated
  `boredom_buster()` function, `_build_agent()` which constructs a
  fresh Strands `Agent` + its 5 tools per invocation (search_movies,
  search_tv_shows, check_history, record_feedback,
  ask_clarifying_question).
- `tmdb_client.py` -- thin TMDB (themoviedb.org) API wrapper: movie/TV
  search, poster URL construction, trailer lookup.
- `memory_client.py` -- AgentCore Memory read/write helper
  (`BoredomBusterMemory`), wrapping `bedrock_agentcore.memory.
  MemoryClient` directly (not the Strands session-manager integration
  -- see the module's own docstring for why a more targeted approach
  fits this agent's specific memory needs better than "remember every
  turn transparently").
- `requirements.txt` -- pinned `bedrock-agentcore` and `strands-agents`
  SDK versions.
- `tests/` -- unit tests with all AWS/TMDB calls mocked (no real
  network/AWS access during `pytest`).

## Response contract

`alexa_lambda/bedrock_client.py`'s `invoke_agentcore_task()` requires
every AgentCore agent used by this project to return a
`{"response_text", "input_tokens", "output_tokens"}` JSON shape. This
agent's response adds one more field beyond that base contract:

```json
{
  "response_text": "...",
  "input_tokens": 42,
  "output_tokens": 128,
  "recommendations": [
    {
      "id": 550,
      "media_type": "movie",
      "title": "Fight Club",
      "overview": "...",
      "release_date": "1999-10-15",
      "rating": 8.4,
      "poster_url": "https://image.tmdb.org/t/p/w500/....jpg",
      "trailer_url": "https://www.youtube.com/watch?v=..."
    }
  ]
}
```

`recommendations` carries the structured data the Echo Show's APL
recommendations grid needs -- cover art, synopsis, rating, and a
trailer link -- since Boredom Buster's whole point is a visual
recommendation experience, not just a spoken sentence.

## Multi-turn conversations (clarifying questions)

`ask_clarifying_question_tool` lets the agent ask a follow-up (e.g.
"movie or TV show?") when a request is too vague to search on. For that
follow-up to actually work as a real back-and-forth, two things have to
hold across the two turns:

1. **The Alexa session must stay open** between the question and the
   user's answer -- `alexa_lambda/handlers/handler.py` keeps
   `shouldEndSession=false` for `BoredomBusterIntent` specifically (every
   other intent still ends its session after one turn, unchanged).
2. **The agent must remember it already asked the question** -- `agent.py`'s
   `_build_agent()` caches one Strands `Agent` per `session_id` at
   module level (`_session_agents`), rather than constructing a brand-new,
   memoryless `Agent` on every invocation. A Strands `Agent` keeps its
   own turn history in its `messages` attribute for as long as the same
   instance keeps being reused, and AgentCore Runtime's documented
   session affinity routes repeated calls sharing the same
   `runtimeSessionId` to the same warm execution environment -- so the
   cache actually gets hit turn-to-turn within one Alexa session, not
   just in theory. Without this, the agent had no idea it had already
   asked a clarifying question, so per its own "don't ask twice" system
   prompt rule it just guessed a mood itself on the next turn, ignoring
   whatever the user actually said.

`collected_recommendations` (the per-turn side-channel list the search
tools populate) is cleared, not rebuilt, on a cache hit, so stale results
from a previous turn never leak into the current turn's response while
the cached `Agent`/memory objects persist correctly.

## Request contract

`alexa_lambda/router.py` calls this agent with:

```json
{"input": "<spoken request>", "actor_id": "<Alexa userId>", "session_id": "<Alexa sessionId>"}
```

`actor_id` (Alexa's stable per-user `userId`) is what AgentCore
Memory's long-term SEMANTIC strategy keys on for per-user preferences.
`session_id` (Alexa's per-conversation `sessionId`) scopes both
AgentCore Runtime's session affinity and the SUMMARIZATION strategy's
short-term conversation continuity. These are kept as two distinct IDs
throughout the pipeline -- see `router.py`'s `route_request()`
docstring for the full reasoning.

## Environment variables (set automatically by Terraform)

Unlike a typical AgentCore Runtime tutorial (where you set these by hand
via the AgentCore CLI's configuration), every environment variable below
is set directly by `terraform/agentcore_runtime.tf`'s
`aws_bedrockagentcore_agent_runtime.boredom_buster` resource -- nothing
to configure manually after `terraform apply`:

- `BEDROCK_MODEL_ID` -- from `var.bedrock_model_id` (defaults to
  `amazon.nova-lite-v1:0`).
- `BEDROCK_MAX_TOKENS` -- from `var.bedrock_max_tokens` (defaults to
  `512`).
- `BEDROCK_AGENTCORE_MEMORY_ID` -- the real Memory resource's ID
  (`terraform/agentcore_memory.tf`), known within the same `terraform
  apply` that creates this agent -- no manual copy-paste.
- `TMDB_API_KEY_PARAMETER_NAME` -- the *name* of the SSM Parameter
  Store SecureString parameter holding the actual TMDB key
  (`terraform/agentcore_memory.tf`'s `aws_ssm_parameter.
  tmdb_api_key`), not the key value itself. `tmdb_client.py`'s
  `_get_api_key()` calls `ssm:GetParameter` with `WithDecryption=True`
  at request time to resolve the real value -- this keeps the actual
  secret out of this resource's `environment_variables` (which would
  otherwise be visible in Terraform state and the AgentCore console).
  Get a free TMDB key at
  [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
  and set it via `TF_VAR_tmdb_api_key` before running `terraform
  apply` -- see the repo root `README.md`'s TMDB API key step.

- `TMDB_WATCH_REGION` -- from `var.tmdb_watch_region` (defaults to
  `AU`). A two-letter ISO-3166-1 country code used for the detail
  screen's "Where to watch" row (`tmdb_client.get_watch_providers`).
  Streaming rights differ per country, so TMDB's watch-providers
  endpoint has no global answer -- set this to wherever the Echo Show
  actually is, or the provider row will be empty for titles that are
  streaming locally. It is only a query parameter, so it costs nothing
  either way.

(For quick local runs outside AgentCore Runtime, e.g. `python3
agent.py` or `pytest`, `tmdb_client.py` also accepts a plain
`TMDB_API_KEY` env var set directly to the key value -- see that
module's `_get_api_key()` docstring. Terraform never sets `TMDB_API_KEY`
directly on the deployed agent, only `TMDB_API_KEY_PARAMETER_NAME`.)

## What the spoken reply does and doesn't contain

When a turn produces recommendations, the model's own final text is
**discarded** and replaced with a short generated line (`agent.py`'s
`_build_spoken_summary`) that names at most two titles. This is
deliberate, and it is the fix for a real complaint from device testing:
the model routinely narrated its own tool use ("I searched for comedies
and found several options, let me check your history...") and then read
every title and synopsis aloud, which is slow to listen to and redundant
with the grid already on screen. `SYSTEM_PROMPT` also instructs against
that narration, but an LLM's free text can't be guaranteed -- generating
the line here makes it structurally impossible for reasoning to reach
the user.

The screen is where the titles, ratings, poster art, trailer QR code and
"Where to watch" providers appear -- see
`alexa_lambda/utils/apl.py`.

On turns that produce NO recommendations (a clarifying question, or
nothing matched), the model's text IS passed through as-is -- there's
nothing else to say in that case.

## Deploying to a real AgentCore Runtime

Fully Terraform-managed -- no `agentcore` CLI, no ECR, no container
build. `terraform/agentcore_runtime.tf` uses the AWS provider's native
`aws_bedrockagentcore_agent_runtime` resource with **direct code
deployment** (a `.zip` of this agent's code + dependencies, uploaded to
S3 -- not a container image):

1. `scripts/build_agentcore_package.py` (invoked automatically via a
   `null_resource` + `local-exec` step) installs this directory's
   `requirements.txt` as ARM64-compatible wheels (AgentCore Runtime
   only supports arm64) into a build directory, copies `agent.py`/
   `memory_client.py`/`tmdb_client.py` on top, and zips the result.
2. The zip is uploaded to a dedicated, private S3 bucket
   (`aws_s3_bucket.agent_packages`).
3. `aws_bedrockagentcore_agent_runtime.boredom_buster` is created/
   updated pointing at that S3 object, with its own least-privilege
   execution role and every environment variable above set directly.
4. `task_registry/task_registry.json.tpl`'s `BoredomBuster.
   agent_runtime_arn` is filled in automatically from this resource's
   real `agent_runtime_arn` attribute, in the same `terraform apply` --
   no manual ARN paste, ever.

Just run `terraform apply` from `terraform/` (see the repo root
`README.md` for the one prerequisite -- a free TMDB API key). A code
change to `agent.py`/`memory_client.py`/`tmdb_client.py`/
`requirements.txt` triggers a rebuild + redeploy on the next apply
automatically, via the build step's file-hash triggers.
