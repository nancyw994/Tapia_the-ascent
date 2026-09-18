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
# While it is still "xxx", the key is read from the environment, repo .env, or ~/.hermes/.env.
# Never commit a real key.
OPENROUTER_API_KEY = "xxx"


def _read_key_from_env_file(path: Path) -> tuple[str, str]:
    """Return (key, status) where status is missing|empty|ok."""
    if not path.is_file():
        return "", "missing"
    found_blank = False
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        name, _, value = s.partition("=")
        if name.strip() != "OPENROUTER_API_KEY":
            continue
        key = value.strip().strip("\"'")
        if key and key != "xxx":
            return key, "ok"
        found_blank = True
    return "", "empty" if found_blank else "missing"


def _load_api_key() -> str:
    """Constant above, then process env, then local .env files, then ~/.hermes/.env."""
    if OPENROUTER_API_KEY and OPENROUTER_API_KEY != "xxx":
        return OPENROUTER_API_KEY
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key
    root = Path(__file__).resolve().parents[1]
    saw_empty = False
    for path in (
        Path.cwd() / ".env",
        root / ".env",
        root / "fact-checker" / ".env",
        Path.home() / ".hermes" / ".env",
    ):
        key, status = _read_key_from_env_file(path)
        if status == "ok":
            return key
        if status == "empty":
            saw_empty = True
    if saw_empty:
        raise RuntimeError(
            "Found .env but OPENROUTER_API_KEY is empty. Open .env in the project root "
            "and paste your key right after the equals sign, then restart the server."
        )
    raise RuntimeError(
        "OPENROUTER_API_KEY is not set. Paste it into the gitignored .env next to README "
        "(OPENROUTER_API_KEY=sk-or-...) or export it in the same terminal as the server."
    )


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
