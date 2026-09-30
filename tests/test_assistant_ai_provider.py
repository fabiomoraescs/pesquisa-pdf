"""Contrato do provider OpenAI sem chamadas de rede."""

import unittest

from platform_core.assistant_ai_provider import AIProviderError, OpenAIProvider


class CaptureResponses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class RateLimitError(Exception):
    code = "credit_balance_exhausted"


class Client:
    def __init__(self, responses):
        self.responses = responses


class AssistantAIProviderTests(unittest.TestCase):
    def test_responses_request_disables_store_and_uses_configured_model(self):
        responses = CaptureResponses(response={"output_text": "ok"})
        provider = OpenAIProvider(api_key="test-key", model="model-test")
        provider._client = Client(responses)
        self.assertEqual(provider.generate(
            instructions="instruções", input_items=[{"role": "user", "content": "oi"}],
            tools=[{"type": "function", "name": "get_platform_help", "parameters": {}}],
        )["output_text"], "ok")
        self.assertEqual(responses.calls[0]["model"], "model-test")
        self.assertFalse(responses.calls[0]["store"])
        self.assertFalse(responses.calls[0]["parallel_tool_calls"])
        self.assertEqual(responses.calls[0]["tool_choice"], "auto")

    def test_responses_required_choice_is_limited_to_server_allowed_functions_and_continuation_is_auto(self):
        responses = CaptureResponses(response={"output": []})
        provider = OpenAIProvider(api_key="test-key", model="model-test")
        provider._client = Client(responses)
        initial = provider.generate(
            instructions="instruções", input_items=[{"role": "user", "content": "oi"}],
            tools=[{"type": "function", "name": "get_platform_help", "parameters": {}}],
            tool_mode="required", allowed_tool_names=("get_platform_help",),
        )
        self.assertEqual(responses.calls[0]["tool_choice"], {
            "type": "allowed_tools", "mode": "required",
            "tools": [{"type": "function", "name": "get_platform_help"}],
        })
        provider.continue_with_tool_outputs(
            instructions="instruções", input_items=[{"role": "user", "content": "oi"}], response=initial,
            tool_outputs=[], tools=[{"type": "function", "name": "get_platform_help", "parameters": {}}],
        )
        self.assertEqual(responses.calls[1]["tool_choice"], "auto")

    def test_credit_exhaustion_has_a_safe_specific_message(self):
        provider = OpenAIProvider(api_key="test-key")
        provider._client = Client(CaptureResponses(error=RateLimitError()))
        with self.assertRaises(AIProviderError) as raised:
            provider.generate(instructions="x", input_items=[], tools=[])
        self.assertEqual(raised.exception.public_message,
                         "O limite de uso da IA foi atingido neste momento. Tente novamente mais tarde.")

    def test_missing_key_does_not_construct_a_client(self):
        with self.assertRaises(AIProviderError) as raised:
            OpenAIProvider(api_key="")
        self.assertEqual(raised.exception.public_message,
                         "O Assistente por IA ainda não está configurado neste ambiente.")


if __name__ == "__main__":
    unittest.main()
