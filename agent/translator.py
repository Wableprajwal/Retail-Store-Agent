"""
Format translator between LLM providers.

Tools are defined ONCE, in Anthropic format (agent/tool_definitions.py) — that
is the canonical schema. Providers speak different dialects on the wire:

  - Anthropic: {"name", "description", "input_schema"} + tool_use/tool_result blocks
  - OpenAI/Ollama: {"type": "function", "function": {...}} + tool_calls/role="tool"

This module converts the canonical tool schema to each provider's dialect, and
adapts the system prompt where a model family needs extra steering (small local
models tend to describe actions instead of calling tools unless told firmly).
"""


def tools_to_gemini(tools):
    """Anthropic tool definitions -> Gemini native function declarations.

    Gemini rejects empty parameter schemas, so tools with no inputs omit
    the parameters field entirely.
    """
    decls = []
    for t in tools:
        decl = {"name": t["name"], "description": t.get("description", "")}
        schema = t.get("input_schema", {})
        if schema.get("properties"):
            decl["parameters"] = schema
        decls.append(decl)
    return decls


def tools_to_openai(tools):
    """Anthropic tool definitions -> OpenAI/Ollama function-calling format."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


# Small local models often narrate ("I would call create_order...") instead of
# emitting a tool call. This addendum pushes them to actually invoke tools.
_LOCAL_MODEL_ADDENDUM = """

## Tool calling (strict)
- To take ANY action or fetch ANY data, you MUST emit a tool call. Never write
  the tool name or arguments as plain text, and never say what you "would" do.
- Only use the tools provided. Never invent tool names or parameters.
- Arguments must be valid JSON matching the tool's schema exactly.
- When the user names an order ID (like O-1006), use EXACTLY that ID — never
  substitute a different order from earlier in the conversation.
- If a request has multiple steps, complete ALL of them with tool calls before
  responding. Never stop to ask "shall I proceed?" — just proceed.
- If a parameter can be inferred from the rules, use it instead of asking
  (e.g. "last month" = month "2026-05", "today" = 2026-06-19).
- After the final tool result, reply with a short plain-text summary for the user.
"""


def adapt_system_prompt(base_prompt, provider):
    """Return the system prompt tuned for the target provider."""
    if provider == "ollama":
        return base_prompt + _LOCAL_MODEL_ADDENDUM
    return base_prompt
