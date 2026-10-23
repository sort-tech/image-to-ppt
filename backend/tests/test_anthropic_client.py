"""The Claude adapter: chat-completions in, Messages API out - fully offline."""

from types import SimpleNamespace

from app import anthropic_client
from app.anthropic_client import AnthropicChatClient, to_messages_request


def _messages():
    return [
        {"role": "system", "content": "You are a slide analyzer."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Analyze this slide."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
        },
    ]


class TestRequestTranslation:
    def test_system_moves_to_the_top_level_and_images_become_base64_blocks(self):
        request = to_messages_request("claude-opus-5-5", _messages())
        assert request["system"] == "You are a slide analyzer."
        assert [m["role"] for m in request["messages"]] == ["user"]
        text, image = request["messages"][0]["content"]
        assert text == {"type": "text", "text": "Analyze this slide."}
        assert image == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        }

    def test_a_plain_url_becomes_a_url_source(self):
        block = anthropic_client._image_block("https://example.com/slide.png")
        assert block["source"] == {"type": "url", "url": "https://example.com/slide.png"}

    def test_json_mode_becomes_a_system_instruction(self):
        request = to_messages_request(
            "claude-opus-5-5", _messages(), response_format={"type": "json_object"}
        )
        assert request["system"].endswith(anthropic_client._JSON_ONLY)
        assert "response_format" not in request

    def test_sampling_parameters_are_never_sent(self):
        """The Messages API rejects temperature on every current model."""
        sent = {}

        class FakeMessages:
            def create(self, **kwargs):
                sent.update(kwargs)
                return SimpleNamespace(content=[], stop_reason="end_turn", model="m")

        completions = anthropic_client._Completions(
            SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
        )
        completions.create(model="claude-opus-5-5", messages=_messages(), temperature=0.1)
        assert "temperature" not in sent

    def test_current_models_get_effort_and_server_side_fallbacks(self):
        request = to_messages_request("claude-opus-5-5", _messages())
        assert request["output_config"] == {"effort": "medium"}
        assert request["fallbacks"] == "default"
        assert request["betas"] == ["server-side-fallback-2026-07-01"]

    def test_other_models_are_sent_without_either(self):
        request = to_messages_request("claude-haiku-4-5", _messages())
        assert "fallbacks" not in request and "betas" not in request
        assert "output_config" not in request


class TestResponseTranslation:
    def test_only_text_blocks_reach_the_caller(self):
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text='{"elements": []}'),
            ],
            stop_reason="end_turn",
            model="claude-opus-5-5",
        )
        choice = anthropic_client._as_chat_completion(response).choices[0]
        assert choice.message.content == '{"elements": []}'
        assert choice.finish_reason == "stop"

    def test_a_refusal_is_reported_as_one_not_as_empty_content(self):
        from app import vision_analyzer

        response = SimpleNamespace(
            content=[],
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber"),
            model="claude-opus-5-5",
        )
        completion = anthropic_client._as_chat_completion(response)
        try:
            vision_analyzer._message_content(completion)
        except vision_analyzer.VisionAnalysisError as exc:
            assert "refused" in str(exc) and "cyber" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a refusal must raise")

    def test_max_tokens_maps_to_length(self):
        response = SimpleNamespace(content=[], stop_reason="max_tokens", model="m")
        assert anthropic_client._as_chat_completion(response).choices[0].finish_reason == "length"


def test_the_client_builds_offline():
    client = AnthropicChatClient(api_key="a-test-key", timeout=5.0, max_retries=0)
    assert client.api_key == "a-test-key"
    assert client.max_retries == 0
