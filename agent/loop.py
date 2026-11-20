import copy
import json
import time
import uuid

from .prompts import SYSTEM_PROMPT
from .tool_definitions import TOOLS
from . import tools
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


def run_turn(client, history, user_text, dispatch_fn, turn_id, max_steps=12, log=print):
    """Run one user turn: call the LLM until it stops requesting tools.

    dispatch_fn(name, input, ctx) -> result dict, ctx = {"call_id", "turn_id"}.
    Returns {"text": str, "tool_calls": [{"name", "input", "result", "call_id"}]}.
    """
    client.append_user(history, user_text)
    calls = []
    text = ""

    for _ in range(max_steps):
        try:
            response = _chat_with_retry(client, history)
        except Exception as e:
            log(f"\n[LLM request failed: {e}]\n")
            break

        client.append_assistant(history, response)

        if client.is_tool_use(response):
            tool_results = []
            for tc in client.get_tool_calls(response):
                log(f"  [tool: {tc['name']}({json.dumps(tc['input'])})]")
                ctx = {"call_id": tc["id"], "turn_id": turn_id}
                result = dispatch_fn(tc["name"], tc["input"], ctx)
                calls.append({"name": tc["name"], "input": tc["input"],
                              "result": result, "call_id": tc["id"]})
                tool_results.append({"id": tc["id"], "content": json.dumps(result)})
            client.append_tool_results(history, tool_results)

        elif client.is_end_turn(response):
            text = client.get_text(response)
            if text:
                log(f"\nAgent: {text}\n")
            break

        else:
            log("[No further response from model]")
            break

    return {"text": text, "tool_calls": calls}


def _baseline_dispatch(name, tool_input, ctx):
    # deepcopy: create_order mutates its items in place
    return tools.dispatch(name, copy.deepcopy(tool_input))


def run_agent_loop(provider=None):
    client = get_client(provider)
    conversation_history = []

    try:
        from guard.dispatch import make_dispatch
        dispatch_fn = make_dispatch(guard=True)
    except ImportError:
        dispatch_fn = _baseline_dispatch

    session = uuid.uuid4().hex[:6]
    turn_n = 0

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

        turn_n += 1
        run_turn(client, conversation_history, user_input, dispatch_fn,
                 f"cli-{session}-{turn_n}")
