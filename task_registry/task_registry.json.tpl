{
  "_comment": "Maps Alexa intent names to one of three backend types -- 'foundation_model' (Nova, via invoke_model), 'lambda' (a plain AWS Lambda function, via lambda.invoke), or 'agentcore_task' (a Bedrock AgentCore Runtime agent, via invoke_agent_runtime). See solution-design.md Section 11 for the extensibility design. NOTE: TakeNote/SearchKnowledgeBase are intentionally absent from this registry AND from the interaction model (see skill_package/interactionModels/custom/en-US.json) -- they would be placeholder entries with no real agent behind them, which would mean advertising them in the launch message and interaction model misleads users into intents that can't work. Add them back to both files together, only once a real AgentCore agent exists for each -- see DEPLOYMENT.md's 'adding a brand-new intent' section for the 3-file checklist (interaction model, registry, router mapping).",

  "AskBedrock": {
    "type": "foundation_model",
    "description": "Free-form Q&A, routed directly to the default Nova model via invoke_model. No Lambda-to-Lambda call or AgentCore Runtime involved.",
    "parameters": []
  },

  "GetWeather": {
    "type": "lambda",
    "description": "Real, working weather lookup via the free Open-Meteo API, running as a plain Lambda function (lambda_tasks/get_weather/handler.py) -- deliberately NOT AgentCore, since a deterministic API lookup doesn't need AgentCore's containerized-agent machinery.",
    "lambda_function_arn": "${get_weather_task_lambda_arn}",
    "parameters": ["location"]
  },

  "BoredomBuster": {
    "type": "agentcore_task",
    "description": "This project's real Bedrock AgentCore showcase: movie/TV recommendations via TMDB search, with multi-tool reasoning (search, clarifying questions, feedback recording) and long-term per-user memory. Fully Terraform-managed via terraform/agentcore_runtime.tf's aws_bedrockagentcore_agent_runtime -- agent_runtime_arn below is real, substituted at apply time, not a manual paste.",
    "agent_runtime_arn": "${boredom_buster_agent_runtime_arn}",
    "parameters": ["mood_or_genre"]
  },

  "ViewWatchlist": {
    "type": "lambda",
    "description": "Shows the user's watchlist - a lightweight local DynamoDB read, handled directly in skill Lambda (no LLM/AgentCore call).",
    "lambda_function_arn": "local",
    "parameters": []
  }
}