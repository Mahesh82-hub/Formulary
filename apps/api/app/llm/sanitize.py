"""Removes chat-protocol fragments that a model leaks into its visible answer.

GPT-OSS models use the "harmony" message format internally. Occasionally the model starts a
tool call mid-answer and the call arrives as text instead of a structured tool call:

    ...marketed by Kenvue【functions.get_drug_composition to=assistant<|channel|>commentary
    <|constrain|>json<|message|>{"drug":"Tylenol Extra Strength", ...}

That is never meant for a reader. The fragment - from its start through the end of its JSON
payload - is removed, as are any stray control tokens such as <|channel|>.
"""

from __future__ import annotations

import re

CONTROL_TOKEN = re.compile(r"<\|[a-z_]+\|>")
LEAK_START = re.compile(r"【?\s*(?:functions\.[\w.-]+\s*)?to=[\w.-]+\s*<\|channel\|>")
MESSAGE_TOKEN = "<|message|>"
MAX_LEAKS = 5


def _balanced_end(text: str, start: int) -> int:
    """Index just past the JSON object opening at ``start``, or the end of its line."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    newline = text.find("\n", start)
    return len(text) if newline == -1 else newline


def strip_protocol_leaks(text: str) -> tuple[str, bool]:
    """Return the text without leaked protocol fragments, and whether any were found."""
    found = False
    for _ in range(MAX_LEAKS):
        match = LEAK_START.search(text)
        if match is None:
            break
        found = True
        end = match.end()
        message = text.find(MESSAGE_TOKEN, end)
        if message != -1:
            cursor = message + len(MESSAGE_TOKEN)
            while cursor < len(text) and text[cursor] in " \t":
                cursor += 1
            if cursor < len(text) and text[cursor] == "{":
                end = _balanced_end(text, cursor)
            else:
                newline = text.find("\n", cursor)
                end = len(text) if newline == -1 else newline
        text = text[: match.start()].rstrip(" \t") + text[end:]
    if CONTROL_TOKEN.search(text):
        found = True
        text = CONTROL_TOKEN.sub("", text)
    return text, found
