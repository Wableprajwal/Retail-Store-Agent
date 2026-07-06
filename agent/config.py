"""
Configuration: loads .env and decides which LLM provider to use.

Provider selection order:
  1. LLM_PROVIDER in the environment / .env (explicit wins)
  2. ANTHROPIC_API_KEY present  -> anthropic
  3. OPENAI_API_KEY present     -> openai
  4. GEMINI_API_KEY present     -> gemini
  5. otherwise                  -> ollama (local, no key needed)
"""

import os

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")


def load_env(path=ENV_PATH):
    """Tiny .env loader — no external dependency. Real env vars always win."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def detect_provider():
    explicit = os.environ.get("LLM_PROVIDER")
    if explicit:
        return explicit.lower()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return "ollama"
