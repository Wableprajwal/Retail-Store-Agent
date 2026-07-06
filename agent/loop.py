import json
import time

from .prompts import SYSTEM_PROMPT
from .tool_definitions import TOOLS
from .tools import dispatch
from .llm import get_client


def _chat_with_retry(client, history, retries=2):
    """One retry with backoff for transient API errors (rate limit, 5xx)."""
    for attempt in range(retries + 1):
        try:
            return client.chat(SYSTEM_PROMPT, history, TOOLS)
        except Exception as e:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"  [API error: {e} — retrying in {wait}s]")
            time.sleep(wait)


def run_agent_loop(provider=None):
    client = get_client(provider)
    conversation_history = []

    print(f"Retail Store Agent ready [{client.provider_name()}]")
    print("Type your instruction ('exit' or Ctrl+C to quit).\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break

        client.append_user(conversation_history, user_input)

        # Agentic loop: keep calling the LLM until it stops requesting tools
        while True:
            try:
                response = _chat_with_retry(client, conversation_history)
            except Exception as e:
                print(f"\n[LLM request failed: {e}]\n")
                break

            client.append_assistant(conversation_history, response)

            if client.is_tool_use(response):
                tool_calls = client.get_tool_calls(response)
                tool_results = []
                for tc in tool_calls:
                    print(f"  [tool: {tc['name']}({json.dumps(tc['input'])})]")
                    result = dispatch(tc["name"], tc["input"])
                    tool_results.append({"id": tc["id"], "content": json.dumps(result)})
                client.append_tool_results(conversation_history, tool_results)

            elif client.is_end_turn(response):
                text = client.get_text(response)
                if text:
                    print(f"\nAgent: {text}\n")
                break

            else:
                print("[No further response from model]")
                break
