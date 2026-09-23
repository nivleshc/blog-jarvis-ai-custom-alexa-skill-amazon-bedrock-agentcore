# GetWeather -- Classic Lambda Task

The `GetWeather` task, running as a plain AWS Lambda function on the task
registry's `lambda` backend type. A deterministic weather lookup has no
decision-making, tool selection, or memory involved, so it doesn't need
AgentCore's containerized-agent machinery -- `BoredomBuster` (a real
multi-tool, memory-backed AgentCore agent) is this project's genuine
AgentCore showcase instead.

## What it does

Looks up current weather conditions for a spoken location using
[Open-Meteo](https://open-meteo.com/) -- a free, keyless weather API. No
Bedrock model call happens here: current weather is a deterministic
lookup, not something that benefits from an LLM's reasoning, so
`input_tokens`/`output_tokens` are correctly always `0` for this task
(`SkillCallLog` records $0 model cost for `GetWeather` calls, and this
runs as a plain Lambda invocation billed at ordinary Lambda rates).

## Files

- `handler.py` -- the Lambda function. Plain `lambda_handler(event,
  context)` entrypoint, plus the geocoding/forecast logic factored out for
  direct unit testing.
- `tests/test_handler.py` -- unit tests with all HTTP calls mocked (no
  real network access during `pytest`).

## Response contract

`alexa_lambda/bedrock_client.py`'s `invoke_lambda_task()` requires every
Lambda-backed task used by this project to return exactly this JSON
shape:

```json
{"response_text": "...", "input_tokens": 0, "output_tokens": 0}
```

This handler always returns that shape, including on error (e.g. an
unresolvable location produces a clean spoken sentence, not an
exception propagating out as a Lambda `FunctionError`).

## Deployment

This function is deployed via Terraform (`terraform/lambda_tasks.tf`) as
an ordinary `aws_lambda_function` resource -- no AgentCore CLI, no ECR,
no separate deploy step. `task_registry/task_registry.json`'s `GetWeather`
entry points at this function's ARN via `lambda_function_arn`.
