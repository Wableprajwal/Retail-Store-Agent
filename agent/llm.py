"""
LLM provider abstraction.

Three providers behind one interface (set LLM_PROVIDER, or let it auto-detect
from whichever credentials exist — see agent/config.py):

  anthropic  Claude via the Anthropic SDK    needs ANTHROPIC_API_KEY
             model: ANTHROPIC_MODEL          (default: claude-sonnet-4-6)
  openai     ChatGPT via the OpenAI SDK      needs OPENAI_API_KEY
             model: OPENAI_MODEL             (default: gpt-4o)
  gemini     Gemini via google-genai SDK     needs GEMINI_API_KEY (free at
             (native endpoint — supports     aistudio.google.com/apikey)
             the new 'AQ.'-prefix keys)      model: GEMINI_MODEL (default: gemini-2.5-flash)
  ollama     any local model via Ollama      needs `ollama serve` running
             model: OLLAMA_MODEL             (default: qwen2.5:7b)
             url:   OLLAMA_BASE_URL          (default: http://localhost:11434/v1)

Every client exposes the same methods, so the agent loop (agent/loop.py) is
provider-agnostic:

  chat(system, history, tools) -> response
  append_assistant(history, response)
  append_tool_results(history, tool_results)
  get_tool_calls(response) -> [{"id", "name", "input"}]
  get_text(response) -> str
  is_tool_use(response) / is_end_turn(response)
  provider_name() -> str

Good Ollama models for tool calling: qwen2.5:7b, llama3.1:8b, mistral-nemo
"""

import json
import os

from .config import detect_provider
from .translator import tools_to_openai, tools_to_gemini, adapt_system_prompt


class AnthropicClient:
    provider_key = "anthropic"

    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def append_user(self, history, text):
        history.append({"role": "user", "content": text})

    def chat(self, system, history, tools):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=adapt_system_prompt(system, self.provider_key),
            tools=tools,  # canonical format IS the Anthropic format
            messages=history,
        )
        return {"_raw": response}

    def append_assistant(self, history, response):
        history.append({"role": "assistant", "content": response["_raw"].content})

    def append_tool_results(self, history, tool_results):
        history.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
                for r in tool_results
            ],
        })

    def get_tool_calls(self, response):
        calls = []
        for block in response["_raw"].content:
            if block.type == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": block.input})
        return calls

    def get_text(self, response):
        for block in response["_raw"].content:
            if block.type == "text":
                return block.text
        return ""

    def is_tool_use(self, response):
        return response["_raw"].stop_reason == "tool_use"

    def is_end_turn(self, response):
        return response["_raw"].stop_reason in ("end_turn", "max_tokens")

    def provider_name(self):
        return f"Claude ({self.model})"


class OpenAICompatClient:
    """Shared implementation for any OpenAI-wire-format endpoint."""

    provider_key = "openai"

    def __init__(self, base_url=None, api_key=None, model="gpt-4o", label="OpenAI"):
        from openai import OpenAI
        kwargs = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self.client = OpenAI(**kwargs)
        self.model = model
        self.label = label

    def append_user(self, history, text):
        history.append({"role": "user", "content": text})

    def chat(self, system, history, tools):
        system = adapt_system_prompt(system, self.provider_key)
        messages = [{"role": "system", "content": system}] + history
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools_to_openai(tools),
        )
        return {"_choice": response.choices[0]}

    def append_assistant(self, history, response):
        msg = response["_choice"].message
        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        history.append(entry)

    def append_tool_results(self, history, tool_results):
        for r in tool_results:
            history.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})

    def get_tool_calls(self, response):
        calls = []
        for tc in response["_choice"].message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            calls.append({"id": tc.id, "name": tc.function.name, "input": args})
        return calls

    def get_text(self, response):
        return response["_choice"].message.content or ""

    def is_tool_use(self, response):
        choice = response["_choice"]
        return bool(choice.message.tool_calls)

    def is_end_turn(self, response):
        choice = response["_choice"]
        return choice.finish_reason in ("stop", "length") and not choice.message.tool_calls

    def provider_name(self):
        return f"{self.label} ({self.model})"


class OpenAIClient(OpenAICompatClient):
    provider_key = "openai"

    def __init__(self):
        super().__init__(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            label="OpenAI",
        )


class GeminiClient:
    """Native Gemini API via the google-genai SDK.

    Google's new 'AQ.'-prefix API keys only work reliably on the native
    endpoint (the OpenAI-compatible shim rejects or misroutes them), so this
    provider speaks Gemini's own wire format: function_declarations in,
    function_call / function_response parts in the content history.
    """

    provider_key = "gemini"

    def __init__(self):
        from google import genai
        from google.genai import types
        self.types = types
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def append_user(self, history, text):
        history.append(self.types.Content(role="user",
                                          parts=[self.types.Part(text=text)]))

    def chat(self, system, history, tools):
        config = self.types.GenerateContentConfig(
            system_instruction=adapt_system_prompt(system, self.provider_key),
            tools=[self.types.Tool(function_declarations=tools_to_gemini(tools))],
        )
        response = self.client.models.generate_content(
            model=self.model, contents=history, config=config,
        )
        return {"_raw": response}

    def _parts(self, response):
        candidates = response["_raw"].candidates or []
        if not candidates or not candidates[0].content:
            return []
        return candidates[0].content.parts or []

    def append_assistant(self, history, response):
        candidates = response["_raw"].candidates or []
        if candidates and candidates[0].content:
            history.append(candidates[0].content)

    def append_tool_results(self, history, tool_results):
        # Gemini matches results to calls by function name, not by id
        parts = [
            self.types.Part.from_function_response(
                name=r["id"], response={"result": r["content"]},
            )
            for r in tool_results
        ]
        history.append(self.types.Content(role="user", parts=parts))

    def get_tool_calls(self, response):
        calls = []
        for part in self._parts(response):
            fc = getattr(part, "function_call", None)
            if fc:
                calls.append({"id": fc.name, "name": fc.name,
                              "input": dict(fc.args or {})})
        return calls

    def get_text(self, response):
        return "".join(p.text for p in self._parts(response)
                       if getattr(p, "text", None))

    def is_tool_use(self, response):
        return bool(self.get_tool_calls(response))

    def is_end_turn(self, response):
        return not self.is_tool_use(response)

    def provider_name(self):
        return f"Gemini ({self.model})"


class OllamaClient(OpenAICompatClient):
    provider_key = "ollama"

    def __init__(self):
        super().__init__(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",  # Ollama ignores it, but the SDK requires one
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
            label="Ollama",
        )


def get_client(provider=None):
    provider = (provider or detect_provider()).lower()
    if provider == "openai":
        return OpenAIClient()
    if provider == "gemini":
        return GeminiClient()
    if provider == "ollama":
        return OllamaClient()
    return AnthropicClient()
