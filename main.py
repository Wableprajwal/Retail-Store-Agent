import os
import sys

from agent.config import load_env, detect_provider


def main():
    load_env()  # pick up credentials from .env (real env vars win)
    provider = detect_provider()

    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: no API key found.")
        print("Put a key in .env (see .env.example), or export one:")
        print("  export ANTHROPIC_API_KEY=sk-ant-...   # Claude")
        print("  export OPENAI_API_KEY=sk-...          # ChatGPT")
        print("Or run fully local: LLM_PROVIDER=ollama python main.py")
        sys.exit(1)
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print("Error: LLM_PROVIDER=openai but OPENAI_API_KEY is not set (env or .env).")
        sys.exit(1)
    if provider == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        print("Error: LLM_PROVIDER=gemini but GEMINI_API_KEY is not set (env or .env).")
        print("Get a free key at https://aistudio.google.com/apikey")
        sys.exit(1)
    if provider == "ollama" and not os.environ.get("LLM_PROVIDER"):
        print("No API key found — falling back to local Ollama.")
        print("(To use Claude / ChatGPT / Gemini, add a key to .env — see .env.example)")
        print("If Ollama isn't installed: get it from https://ollama.com,")
        print("then run: ollama pull qwen2.5:7b\n")

    from agent.seed import seed
    seed()

    from agent.loop import run_agent_loop
    run_agent_loop(provider)


if __name__ == "__main__":
    main()
