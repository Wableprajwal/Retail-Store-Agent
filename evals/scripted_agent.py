"""
Deterministic stand-in for an LLM client (same interface as AnthropicClient).

Per user turn it issues the planned calls one per chat() step, reading each
tool result back out of the history it is given. Like real models it retries
the same call (new call id) after a timeout/malformed error, up to 2 times;
other errors are skipped. Then it ends the turn with a short text.
"""

import json

MAX_RETRIES = 2


class ScriptedClient:
    def __init__(self, turn_scripts):
        """turn_scripts: list (one per user turn) of [{"tool", "input"}, ...]."""
        self.turn_scripts = [list(s) for s in turn_scripts]
        self.turn = -1
        self.queue = []      # planned calls still to issue this turn
        self.pending = None  # {"call", "retries"} awaiting a result
        self.counter = 0

    def provider_name(self):
        return "Scripted (deterministic)"

    def append_user(self, history, text):
        history.append({"role": "user", "content": text})
        self.turn += 1
        script = self.turn_scripts[self.turn] if self.turn < len(self.turn_scripts) else []
        self.queue = [{"call": c, "retries": 0} for c in script]
        self.pending = None

    def _new_call(self, item):
        self.counter += 1
        self.pending = item
        return {"id": f"toolu_scripted_{self.counter}", "name": item["call"]["tool"],
                "input": json.loads(json.dumps(item["call"]["input"]))}

    def _last_result(self, history):
        last = history[-1] if history else None
        if not last or not isinstance(last.get("content"), list):
            return None
        for r in last["content"]:
            if r.get("type") == "tool_result":
                try:
                    return json.loads(r["content"])
                except Exception:
                    return {}
        return None

    def chat(self, system, history, tools):
        if self.pending is not None:
            item, self.pending = self.pending, None
            result = self._last_result(history) or {}
            err = str(result.get("error", "")).lower()
            if ("timeout" in err or "malformed" in err) and item["retries"] < MAX_RETRIES:
                item["retries"] += 1
                return {"call": self._new_call(item)}
        if self.queue:
            return {"call": self._new_call(self.queue.pop(0))}
        return {"text": "Done."}

    def append_assistant(self, history, response):
        if "call" in response:
            c = response["call"]
            content = [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}]
        else:
            content = [{"type": "text", "text": response["text"]}]
        history.append({"role": "assistant", "content": content})

    def append_tool_results(self, history, tool_results):
        history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
            for r in tool_results]})

    def get_tool_calls(self, response):
        return [response["call"]] if "call" in response else []

    def get_text(self, response):
        return response.get("text", "")

    def is_tool_use(self, response):
        return "call" in response

    def is_end_turn(self, response):
        return "call" not in response
