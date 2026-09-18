"""Call an OpenRouter model with a YAML spec plus free text, and return the reply."""

import os
from pathlib import Path

import yaml
from openai import OpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"  # $2 in / $10 out per 1M tokens
# Tried in order by OpenRouter if the primary model is rate-limited or down.
FALLBACK_MODELS = [
    "openai/gpt-5.6-terra",  # $2 / $12
    "deepseek/deepseek-v4-pro-0813",  # $0.58 / $1.73
]


# Teammates: replace "xxx" with your own OpenRouter key (starts with sk-or-).
# While it is still "xxx", the key is read from the environment or ~/.hermes/.env instead.
OPENROUTER_API_KEY = "xxx"


def _load_api_key() -> str:
    """Use OPENROUTER_API_KEY above, else the environment, else Hermes' ~/.hermes/.env."""
    if OPENROUTER_API_KEY and OPENROUTER_API_KEY != "xxx":
        return OPENROUTER_API_KEY
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY" and value.strip():
                return value.strip().strip("\"'")
    raise RuntimeError("OPENROUTER_API_KEY not found in the environment or ~/.hermes/.env")


def ask_llm(
    yaml_spec: str | dict,
    text: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
) -> str:
    """Send `yaml_spec` (instructions/schema) and `text` (user input) to the model.

    `yaml_spec` may be a YAML string or a dict (dumped to YAML). It goes in the
    system message; `text` goes in the user message. Returns the model's reply.
    """
    if isinstance(yaml_spec, dict):
        yaml_spec = yaml.safe_dump(yaml_spec, sort_keys=False, allow_unicode=True)
    else:
        yaml.safe_load(yaml_spec)  # fail early on malformed YAML

    client = OpenAI(api_key=_load_api_key(), base_url=OPENROUTER_BASE_URL)
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {
                "role": "system",
                "content": "Follow the YAML specification below when answering.\n\n" + yaml_spec,
            },
            {"role": "user", "content": text},
        ],
        extra_body={"models": [model, *FALLBACK_MODELS]},
    )
    return response.choices[0].message.content or ""


if __name__ == "__main__":
    spec = """
task: extract_bay_adjustment
output_format: json
fields: [bay, tier, row, action]
"""
    print(ask_llm(spec, "Move the container in bay 12 row 3 tier 2 to bay 14."))
