from app.llm import _as_chat_completion, _responses_tools, _split_messages, _text_format, reasoning_payload


def test_reasoning_payload_shape():
    payload = reasoning_payload()
    assert payload["mode"] == "standard"
    assert payload["summary"] == "auto"
    assert payload["effort"]


def test_split_messages_moves_system_to_instructions():
    instructions, items = _split_messages(
        [
            {"role": "system", "content": "You are staff."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
    )
    assert instructions == "You are staff."
    assert items == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]


def test_responses_tools_unwrap_chat_completions_function():
    tools = _responses_tools(
        [{"type": "function", "function": {"name": "search_tools", "description": "Find tools", "parameters": {"type": "object"}}}]
    )
    assert tools == [
        {
            "type": "function",
            "name": "search_tools",
            "description": "Find tools",
            "parameters": {"type": "object"},
        }
    ]


def test_text_format_json_and_plain():
    assert _text_format({"type": "json_object"})["format"]["type"] == "json_object"
    assert _text_format(None)["format"]["type"] == "text"
    assert _text_format(None)["verbosity"] == "medium"


def test_as_chat_completion_keeps_caller_shape():
    resp = _as_chat_completion("ok")
    assert resp.choices[0].message.content == "ok"
