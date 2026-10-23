"""Claude behind the chat-completions surface the rest of the project calls.

Every caller (``vision_analyzer``, ``services.instructions``) talks to
``client.chat.completions.create(model=..., messages=..., ...)`` and reads
``response.choices[0].message.content``. This module gives them exactly that
surface, backed by the official ``anthropic`` SDK and the Messages API - not an
OpenAI-compatibility shim - so the callers, their retry and their error
classification stay unchanged.

What the translation does, and why:

* ``system`` messages move to the top-level ``system`` field.
* ``image_url`` parts become ``image`` blocks: a ``data:`` URL becomes a base64
  source, anything else a URL source.
* ``temperature`` is dropped: the Messages API rejects sampling parameters on
  every current model.
* ``response_format={"type": "json_object"}`` has no Messages equivalent with
  the same meaning, so it becomes one line of the system prompt; the analyzer's
  JSON recovery handles the rest, as it already does for proxies that ignore
  the parameter.
* A ``refusal`` stop is reported as ``message.refusal``, which the analyzer
  already turns into a named error instead of "empty content".

``anthropic`` is imported lazily, like ``openai`` in :mod:`app.config`, so the
rest of the project (and the test suite) imports without it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

__all__ = ["AnthropicChatClient", "to_messages_request"]

#: Non-streaming output ceiling: room for a dense spec, under the SDK's
#: non-streaming timeout guard.
MAX_TOKENS = 16000

#: Models that take ``fallbacks: "default"``: a request their safety
#: classifiers decline is re-run server-side on Anthropic's recommended model
#: instead of coming back empty. Other models are sent without it.
_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Models that take ``output_config.effort``. Claude Opus 5.5 defaults to
#: ``medium``; it is set explicitly so the cost of a conversion does not move
#: when a default does.
_EFFORT_MODELS = _FALLBACK_MODELS | {"claude-haiku-5-5"}
_EFFORT = "medium"

_JSON_ONLY = "Reply with the JSON object only: no prose, no code fences."


def _image_block(url: str) -> dict:
    if url.startswith("data:") and ";base64," in url:
        header, data = url.split(",", 1)
        media_type = header[len("data:") : header.index(";")]
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_blocks(content: Any) -> list[dict] | str:
    if isinstance(content, str):
        return content
    blocks: list[dict] = []
    for part in content or []:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            blocks.append({"type": "text", "text": str(part.get("text", ""))})
        elif kind == "image_url":
            blocks.append(_image_block(str((part.get("image_url") or {}).get("url", ""))))
    return blocks


def to_messages_request(
    model: str, messages: list[dict], response_format: Any = None
) -> dict:
    """The Messages API keyword arguments for one chat-completions call."""
    system: list[str] = []
    turns: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            system.append(content if isinstance(content, str) else str(content))
        elif role in ("user", "assistant"):
            turns.append({"role": role, "content": _content_blocks(message.get("content"))})
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        system.append(_JSON_ONLY)

    request: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "messages": turns}
    if system:
        request["system"] = "\n\n".join(system)
    if model in _EFFORT_MODELS:
        request["output_config"] = {"effort": _EFFORT}
    if model in _FALLBACK_MODELS:
        request["betas"] = [_FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def _as_chat_completion(response: Any) -> SimpleNamespace:
    """An OpenAI-shaped view of a Messages response: text blocks only."""
    text = "".join(
        getattr(block, "text", "") or ""
        for block in getattr(response, "content", None) or []
        if getattr(block, "type", None) == "text"
    )
    stop = getattr(response, "stop_reason", None)
    refusal = None
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        refusal = f"declined by Claude's safety classifiers ({category or 'no category'})"
        text = None
    finish = {"end_turn": "stop", "max_tokens": "length"}.get(stop, stop)
    message = SimpleNamespace(role="assistant", content=text, refusal=refusal)
    return SimpleNamespace(
        model=getattr(response, "model", None),
        choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)],
        usage=getattr(response, "usage", None),
    )


class _Completions:
    def __init__(self, client: Any) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: Any = None,
        temperature: Any = None,  # noqa: ARG002 - rejected by the Messages API
        **_ignored: Any,
    ) -> SimpleNamespace:
        request = to_messages_request(model, messages, response_format)
        return _as_chat_completion(self._client.beta.messages.create(**request))


class AnthropicChatClient:
    """``client.chat.completions.create`` on top of ``anthropic.Anthropic``."""

    def __init__(self, *, api_key: str, timeout: float, max_retries: int) -> None:
        import anthropic

        self.api_key = api_key
        self.max_retries = max_retries
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        # The SDK honours ANTHROPIC_BASE_URL; report where requests really go.
        self.base_url = str(self._client.base_url)
        self.chat = SimpleNamespace(completions=_Completions(self._client))
