"""Contrato provider-neutral do Assistente Análysis e integrações de IA.

As classes deste módulo apenas traduzem o contrato de tools da aplicação para
cada SDK. Autorização, recuperação documental e execução das tools continuam
fora dos providers.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_GEMINI_ASSISTANT_MODEL = "gemini-3.8-flash"
DEFAULT_OPENAI_ASSISTANT_MODEL = "gpt-4.1"
GEMINI_REQUEST_TIMEOUT_SECONDS = 45.0
OPENAI_REQUEST_TIMEOUT_SECONDS = 45.0
MAX_PROVIDER_RETRIES = 2
GEMINI_PROVIDER_RETRY_DELAYS_SECONDS = (1.0, 3.0)


class AIProviderError(RuntimeError):
    """Erro seguro para exibição, sem payload, prompt ou credencial."""

    def __init__(
        self,
        public_message: str,
        *,
        status_code: int = 503,
        reason_class: str = "unknown",
        fallback_eligible: bool = False,
    ):
        super().__init__(public_message)
        self.public_message = public_message
        self.status_code = status_code
        self.reason_class = reason_class
        self.fallback_eligible = fallback_eligible


class AIProvider(ABC):
    """Contrato pequeno para providers compatíveis com function calling."""

    provider_id = "unknown"

    @abstractmethod
    def generate(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        tool_mode: str = "auto",
        allowed_tool_names: Sequence[str] | None = None,
    ) -> Any:
        raise NotImplementedError

    @abstractmethod
    def continue_with_tool_outputs(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        response: Any,
        tool_outputs: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Any:
        raise NotImplementedError

    def test_connection(self) -> None:
        """Chamada mínima administrativa, sem tools, corpus ou projeto."""
        self.generate(
            instructions="Teste administrativo de disponibilidade. Responda somente 'ok'.",
            input_items=[{"role": "user", "content": "Teste de conexão."}],
            tools=[],
        )


def _safe_provider_error(error: Exception, *, provider: str) -> AIProviderError:
    """Classifica somente a categoria técnica; nunca expõe a resposta bruta."""
    name = type(error).__name__
    raw_code = getattr(error, "code", "") or ""
    raw_status = getattr(error, "status_code", None) or getattr(error, "status", None)
    code = str(raw_code).casefold()
    status = raw_status

    def _http_status(value: object) -> int | None:
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdecimal():
            number = int(value)
            return number if 100 <= number <= 599 else None
        return None

    numeric_status = _http_status(raw_status) or _http_status(raw_code)
    semantic_status = str(raw_status).casefold() if isinstance(raw_status, str) else ""
    semantic_code = code if not code.isdecimal() else semantic_status
    classified: AIProviderError
    if (name in {"AuthenticationError", "PermissionDeniedError", "Unauthorized", "Forbidden"}
            or numeric_status in {401, 403} or semantic_code in {"unauthenticated", "permission_denied", "invalid_api_key"}):
        classified = AIProviderError(
            "O Assistente por IA não está disponível com a configuração atual.",
            reason_class="authentication",
        )
    elif (name in {"RateLimitError", "ResourceExhausted", "TooManyRequests"}
            or numeric_status == 429 or semantic_code in {"resource_exhausted", "quota_exceeded"}):
        classified = AIProviderError(
            "O limite de uso da IA foi atingido neste momento. Tente novamente mais tarde.",
            reason_class="quota",
            fallback_eligible=True,
        )
    elif (name in {"APITimeoutError", "Timeout", "ReadTimeout", "ConnectTimeout", "DeadlineExceeded"}
            or semantic_code == "deadline_exceeded"):
        classified = AIProviderError(
            "Não foi possível alcançar o Assistente por IA neste momento. Tente novamente.",
            reason_class="timeout",
            fallback_eligible=True,
        )
    elif (name in {"APIConnectionError", "ConnectError", "ConnectionError", "ServiceUnavailable", "InternalServerError", "ServerError", "Unavailable"}
            or numeric_status in {500, 502, 503, 504} or semantic_code in {"unavailable", "internal"}):
        classified = AIProviderError(
            "Não foi possível alcançar o Assistente por IA neste momento. Tente novamente.",
            reason_class="unavailable",
            fallback_eligible=True,
        )
    elif name in {"NotFoundError", "ModelNotFound", "InvalidArgument"} or numeric_status == 404 or semantic_code in {"not_found", "model_not_found"}:
        classified = AIProviderError(
            "O modelo configurado para o Assistente não está disponível.",
            reason_class="model_unavailable",
            fallback_eligible=True,
        )
    else:
        classified = AIProviderError(
            "Não foi possível concluir a resposta do Assistente por IA neste momento.",
            reason_class=f"{provider}_unknown",
        )

    # O log preserva somente metadados classificatórios; nunca a mensagem
    # bruta do provider, prompt, saída de tool, corpus ou credencial.
    safe_code = code if code and code.replace("_", "").isalnum() else "unclassified"
    logging.getLogger(__name__).warning(
        "assistant_ai_provider_failure provider=%s exception_type=%s status=%s code=%s reason_class=%s",
        provider,
        name,
        status if isinstance(status, (int, float, str)) else None,
        safe_code,
        classified.reason_class,
    )
    return classified


class OpenAIProvider(AIProvider):
    """Implementação Responses API, com armazenamento remoto desativado."""

    provider_id = "openai"

    def __init__(self, *, api_key: str | None, model: str | None = None,
                 timeout: float = OPENAI_REQUEST_TIMEOUT_SECONDS):
        if not isinstance(api_key, str) or not api_key.strip():
            raise AIProviderError("O Assistente por IA ainda não está configurado neste ambiente.",
                                  reason_class="missing_credential")
        self._api_key = api_key
        self.model = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_OPENAI_ASSISTANT_MODEL
        self.timeout = timeout
        self._client: Any | None = None

    def _responses(self):
        if self._client is not None:
            return self._client.responses
        try:
            from openai import OpenAI
        except ImportError as error:
            raise AIProviderError("O Assistente por IA ainda não está disponível neste ambiente.",
                                  reason_class="sdk_unavailable") from error
        self._client = OpenAI(api_key=self._api_key, timeout=self.timeout)
        return self._client.responses

    def _create(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        tool_mode: str = "auto",
        allowed_tool_names: Sequence[str] | None = None,
    ) -> Any:
        choice: object = "auto"
        allowed = tuple(name for name in (allowed_tool_names or ()) if isinstance(name, str) and name)
        if tool_mode == "required" and allowed:
            choice = {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "function", "name": name} for name in allowed],
            }
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "instructions": instructions,
                "input": list(input_items),
                "tools": list(tools),
                "parallel_tool_calls": False,
                # store=False mantém o conteúdo do turno fora do armazenamento remoto.
                "store": False,
            }
            if tools:
                request["tool_choice"] = choice
            return self._responses().create(**request)
        except AIProviderError:
            raise
        except Exception as error:
            raise _safe_provider_error(error, provider=self.provider_id) from error

    def generate(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        tool_mode: str = "auto",
        allowed_tool_names: Sequence[str] | None = None,
    ) -> Any:
        return self._create(
            instructions=instructions, input_items=input_items, tools=tools,
            tool_mode=tool_mode, allowed_tool_names=allowed_tool_names,
        )

    def continue_with_tool_outputs(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        response: Any,
        tool_outputs: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Any:
        output = getattr(response, "output", None)
        if output is None and isinstance(response, Mapping):
            output = response.get("output", [])
        return self._create(
            instructions=instructions,
            input_items=[*input_items, *(output or []), *tool_outputs],
            tools=tools, tool_mode="auto",
        )


class GeminiProvider(AIProvider):
    """SDK oficial ``google-genai`` com execução manual das tools read-only."""

    provider_id = "gemini"

    def __init__(self, *, api_key: str | None, model: str | None = None,
                 timeout: float = GEMINI_REQUEST_TIMEOUT_SECONDS):
        if not isinstance(api_key, str) or not api_key.strip():
            raise AIProviderError("O Assistente por IA ainda não está configurado neste ambiente.",
                                  reason_class="missing_credential")
        self._api_key = api_key
        self.model = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_GEMINI_ASSISTANT_MODEL
        self.timeout = timeout
        self._client: Any | None = None

    def _sdk(self) -> tuple[Any, Any]:
        try:
            from google import genai
            from google.genai import types
        except ImportError as error:
            raise AIProviderError("O provider Gemini ainda não está disponível neste ambiente.",
                                  reason_class="sdk_unavailable") from error
        return genai, types

    def _models(self) -> Any:
        if self._client is not None:
            return self._client.models
        genai, types = self._sdk()
        self._client = genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=int(self.timeout * 1000)),
        )
        return self._client.models

    @staticmethod
    def _tool_declarations(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters_json_schema": tool.get("parameters", {}),
            }
            for tool in tools
            if isinstance(tool, Mapping) and tool.get("type") == "function" and isinstance(tool.get("name"), str)
        ]

    @staticmethod
    def _contents(input_items: Sequence[Any]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for item in input_items:
            if not isinstance(item, Mapping):
                continue
            role, content = item.get("role"), item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]})
        return contents

    @staticmethod
    def _response_parts(response: Any) -> list[Any]:
        candidates = getattr(response, "candidates", None)
        if not candidates:
            return []
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None)
        return list(parts) if parts else []

    @classmethod
    def _as_common_response(cls, response: Any) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for index, part in enumerate(cls._response_parts(response)):
            call = getattr(part, "function_call", None)
            if call is not None and isinstance(getattr(call, "name", None), str):
                arguments = getattr(call, "args", {}) or {}
                native_call_id = getattr(call, "id", None)
                call_id = (
                    native_call_id.strip()
                    if isinstance(native_call_id, str) and native_call_id.strip()
                    else f"gemini-{index}-{call.name}"
                )
                output.append({
                    "type": "function_call",
                    "name": call.name,
                    # Gemini 3 exige que o id nativo volte no FunctionResponse.
                    # O fallback só atende versões que não retornavam um id.
                    "call_id": call_id,
                    "arguments": json.dumps(dict(arguments), ensure_ascii=False),
                })
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                text_parts.append(text)
        usage = getattr(response, "usage_metadata", None)
        return {
            "output": output,
            "output_text": "\n".join(text_parts).strip() or str(getattr(response, "text", "") or "").strip(),
            "usage": {
                "input_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
            },
            "_gemini_native": response,
        }

    def _create(
        self,
        *,
        instructions: str,
        contents: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        tool_mode: str = "auto",
        allowed_tool_names: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        _genai, types = self._sdk()
        declarations = self._tool_declarations(tools)
        config: dict[str, Any] = {
            "system_instruction": instructions,
            # Desabilita execução automática: as tools ficam sob autorização e
            # auditoria da aplicação, nunca no SDK.
            "automatic_function_calling": {"disable": True},
        }
        if declarations:
            config["tools"] = [types.Tool(function_declarations=declarations)]
            allowed = [name for name in (allowed_tool_names or ()) if isinstance(name, str) and name]
            function_config: dict[str, Any] = {"mode": "ANY" if tool_mode == "required" and allowed else "AUTO"}
            if function_config["mode"] == "ANY":
                function_config["allowed_function_names"] = allowed
            config["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(**function_config),
            )
        started = time.monotonic()
        for attempt in range(MAX_PROVIDER_RETRIES + 1):
            try:
                response = self._models().generate_content(
                    model=self.model,
                    contents=list(contents),
                    config=types.GenerateContentConfig(**config),
                )
                return self._as_common_response(response)
            except AIProviderError:
                raise
            except Exception as error:
                provider_error = _safe_provider_error(error, provider=self.provider_id)
                logging.getLogger(__name__).warning(
                    "assistant_ai_provider_attempt_failed provider=%s attempt=%s reason_class=%s elapsed_ms=%s",
                    self.provider_id,
                    attempt + 1,
                    provider_error.reason_class,
                    round((time.monotonic() - started) * 1000),
                )
                if provider_error.reason_class not in {"unavailable", "timeout"} or attempt >= MAX_PROVIDER_RETRIES:
                    raise provider_error from error
                # Reenvia a mesma requisição ao mesmo provider. Em uma
                # continuação, ``contents`` já inclui FunctionCall e outputs
                # prontos; nenhuma tool é executada novamente.
                time.sleep(GEMINI_PROVIDER_RETRY_DELAYS_SECONDS[attempt])

    def generate(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        tool_mode: str = "auto",
        allowed_tool_names: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self._create(
            instructions=instructions, contents=self._contents(input_items), tools=tools,
            tool_mode=tool_mode, allowed_tool_names=allowed_tool_names,
        )

    def continue_with_tool_outputs(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        response: Any,
        tool_outputs: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        native = response.get("_gemini_native") if isinstance(response, Mapping) else None
        contents = self._contents(input_items)
        if native is not None:
            candidates = getattr(native, "candidates", None)
            native_content = getattr(candidates[0], "content", None) if candidates else None
            if native_content is not None:
                contents.append(native_content)
        _genai, types = self._sdk()
        parts = []
        for item in tool_outputs:
            if not isinstance(item, Mapping):
                continue
            raw = item.get("output", "")
            try:
                result = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                result = {"output": str(raw)}
            call_id = str(item.get("call_id", ""))
            name = next((entry["name"] for entry in _output_calls(response) if entry["call_id"] == call_id), "tool")
            parts.append(types.Part(
                functionResponse=types.FunctionResponse(
                    id=call_id,
                    name=name,
                    response={"result": result},
                )
            ))
        if parts:
            # A resposta da tool é um turno próprio e sucede o conteúdo nativo
            # do modelo, que contém as FunctionCalls e suas assinaturas Gemini.
            # Gemini 3.8 aceita FunctionResponse neste turno ``user``; o
            # endpoint rejeita ``tool`` para este modelo (HTTP 400).
            contents.append(types.Content(role="user", parts=parts))
        return self._create(instructions=instructions, contents=contents, tools=tools, tool_mode="auto")


def _output_calls(response: Any) -> list[dict[str, str]]:
    """Lê chamadas normalizadas sem depender do tipo do SDK de origem."""
    output = response.get("output", []) if isinstance(response, Mapping) else getattr(response, "output", [])
    return [
        {"name": str(item.get("name", "")), "call_id": str(item.get("call_id", ""))}
        for item in output if isinstance(item, Mapping) and item.get("type") == "function_call"
    ]


__all__ = [
    "AIProvider", "AIProviderError", "DEFAULT_GEMINI_ASSISTANT_MODEL", "DEFAULT_OPENAI_ASSISTANT_MODEL",
    "GEMINI_REQUEST_TIMEOUT_SECONDS", "GEMINI_PROVIDER_RETRY_DELAYS_SECONDS", "GeminiProvider",
    "MAX_PROVIDER_RETRIES", "OPENAI_REQUEST_TIMEOUT_SECONDS", "OpenAIProvider",
]
