"""Configuração operacional persistida dos providers do Assistente Análysis.

Esta camada escolhe e instancia providers. Ela não consulta corpus, documentos,
autorização, projetos ou deep links.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from flask import current_app
from sqlalchemy.exc import IntegrityError

from .assistant_ai_provider import (
    AIProvider,
    AIProviderError,
    DEFAULT_GEMINI_ASSISTANT_MODEL,
    DEFAULT_OPENAI_ASSISTANT_MODEL,
    GeminiProvider,
    OpenAIProvider,
)
from .assistant_native_provider import AnalysisNativeProvider, DEFAULT_ANALYSIS_NATIVE_MODEL
from .extensions import db
from .models import AssistantAISettings, AuditLog


STRATEGIES = ("single", "fallback", "task_routing", "parallel")
ACTIVE_STRATEGIES = ("single", "fallback")
FUTURE_ROUTING_TASKS = (
    "help_navigation", "project_methodology", "document_search", "document_summary", "corpus_analysis",
)
PROVIDER_ORDER = ("analysis_native", "gemini", "openai", "anthropic")
CONNECTION_AUDIT_ACTION = "assistant_ai_connection_tested"
CONNECTION_NOT_TESTED = "not_tested"
CONNECTION_AVAILABLE = "available"
CONNECTION_FAILED = "failed"
CONNECTION_NATIVE_AVAILABLE = "native_available"
CONNECTION_STATUS_LABELS = {
    CONNECTION_NOT_TESTED: "Não testado",
    CONNECTION_AVAILABLE: "Conexão disponível",
    CONNECTION_FAILED: "Falha na conexão",
    CONNECTION_NATIVE_AVAILABLE: "Motor nativo disponível",
}
PROVIDERS: dict[str, dict[str, Any]] = {
    "analysis_native": {
        "display_name": "Análysis IA",
        "implemented": True,
        "native": True,
        "credential_config": None,
        "model_field": "analysis_native_model",
        "default_model": DEFAULT_ANALYSIS_NATIVE_MODEL,
        "capabilities": (
            "platform_help", "current_ui", "project_context", "corpus_retrieval",
            "evidence_synthesis", "comparison", "counting", "no_external_knowledge",
            "no_freeform_llm",
        ),
    },
    "gemini": {
        "display_name": "Gemini",
        "implemented": True,
        "credential_config": "GEMINI_API_KEY",
        "model_field": "gemini_model",
        "default_model": DEFAULT_GEMINI_ASSISTANT_MODEL,
    },
    "openai": {
        "display_name": "OpenAI",
        "implemented": True,
        "credential_config": "OPENAI_API_KEY",
        "model_field": "openai_model",
        "default_model": DEFAULT_OPENAI_ASSISTANT_MODEL,
    },
    "anthropic": {
        "display_name": "Claude / Anthropic",
        "implemented": False,
        "credential_config": "ANTHROPIC_API_KEY",
        "model_field": "anthropic_model",
        "default_model": "",
    },
}


class AISettingsValidationError(ValueError):
    """Erro de formulário seguro, associado à configuração e não a segredo."""


def _clean_provider_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip().casefold())
    return result


def _clean_model(value: object, *, provider: str) -> str:
    model = value.strip() if isinstance(value, str) else ""
    if not model and PROVIDERS[provider]["implemented"]:
        raise AISettingsValidationError(f"Informe o modelo configurado para {PROVIDERS[provider]['display_name']}.")
    if len(model) > 160:
        raise AISettingsValidationError("O nome do modelo pode ter no máximo 160 caracteres.")
    return model


def default_settings_values(*, bootstrap_provider: object = None) -> dict[str, Any]:
    """Default seguro; AI_PROVIDER só tem efeito antes do registro persistido."""
    primary = str(bootstrap_provider or "").strip().casefold()
    if primary not in {"analysis_native", "gemini", "openai"}:
        primary = "gemini"
    return {
        "strategy": "single",
        "primary_provider": primary,
        "enabled_providers": [primary],
        "fallback_order": [],
        "analysis_native_model": DEFAULT_ANALYSIS_NATIVE_MODEL,
        "gemini_model": DEFAULT_GEMINI_ASSISTANT_MODEL,
        "openai_model": DEFAULT_OPENAI_ASSISTANT_MODEL,
        "anthropic_model": "",
    }


class AIProviderManager:
    """Resolve providers para cada turno a partir da configuração persistida."""

    def __init__(self, *, app=None):
        self.app = app

    @property
    def _config(self):
        return self.app.config if self.app is not None else current_app.config

    def get_settings(self) -> AssistantAISettings:
        settings = db.session.get(AssistantAISettings, 1)
        if settings is not None:
            return settings
        settings = AssistantAISettings(**default_settings_values(
            bootstrap_provider=self._config.get("AI_PROVIDER"),
        ))
        db.session.add(settings)
        try:
            db.session.flush()
        except IntegrityError:
            # Uma segunda requisição pode ter criado o singleton; consulta de
            # novo após rollback evita criar uma configuração duplicada.
            db.session.rollback()
            settings = db.session.get(AssistantAISettings, 1)
            if settings is None:  # pragma: no cover - proteção para falha incomum do banco
                raise
        return settings

    @staticmethod
    def normalize_configuration(raw: Mapping[str, object]) -> dict[str, Any]:
        strategy = str(raw.get("strategy", "")).strip().casefold()
        if strategy not in ACTIVE_STRATEGIES:
            raise AISettingsValidationError("Selecione uma estratégia de provider disponível.")
        enabled = _clean_provider_list(raw.get("enabled_providers"))
        if not enabled or len(enabled) != len(set(enabled)):
            raise AISettingsValidationError("Selecione providers válidos, sem repetição.")
        unknown = [provider for provider in enabled if provider not in PROVIDERS]
        if unknown:
            raise AISettingsValidationError("A configuração contém um provider desconhecido.")
        not_implemented = [provider for provider in enabled if not PROVIDERS[provider]["implemented"]]
        if not_implemented:
            raise AISettingsValidationError("Providers ainda não implementados não podem ser ativados.")
        primary = str(raw.get("primary_provider", "")).strip().casefold()
        if primary not in enabled:
            raise AISettingsValidationError("O provider principal precisa estar habilitado.")
        if primary not in PROVIDERS or not PROVIDERS[primary]["implemented"]:
            raise AISettingsValidationError("O provider principal não está disponível.")
        fallback = _clean_provider_list(raw.get("fallback_order"))
        if len(fallback) != len(set(fallback)):
            raise AISettingsValidationError("A ordem de fallback não pode repetir providers.")
        if primary in fallback or any(provider not in enabled for provider in fallback):
            raise AISettingsValidationError("O fallback deve conter apenas providers habilitados, diferentes do principal.")
        if any(provider not in PROVIDERS or not PROVIDERS[provider]["implemented"] for provider in fallback):
            raise AISettingsValidationError("A ordem de fallback contém provider indisponível.")
        if strategy == "fallback" and not fallback:
            raise AISettingsValidationError("Escolha ao menos um provider adicional para a estratégia de fallback.")
        values: dict[str, Any] = {
            "strategy": strategy,
            "primary_provider": primary,
            "enabled_providers": enabled,
            "fallback_order": fallback if strategy == "fallback" else [],
        }
        for provider, spec in PROVIDERS.items():
            if spec.get("native"):
                # Esta primeira versão é deliberadamente um motor local fixo,
                # não um seletor de modelos ou uma credencial disfarçada.
                values[spec["model_field"]] = spec["default_model"]
                continue
            model = raw.get(spec["model_field"], spec["default_model"] if spec.get("native") else "")
            if provider in enabled:
                values[spec["model_field"]] = _clean_model(model, provider=provider)
            else:
                cleaned = model.strip() if isinstance(model, str) else ""
                values[spec["model_field"]] = cleaned[:160]
        return values

    @staticmethod
    def normalize_docchat_configuration(raw: Mapping[str, object]) -> dict[str, str | None]:
        """Configuração futura, deliberadamente separada do Assistente atual."""
        provider = str(raw.get("docchat_provider", "")).strip().casefold()
        model = raw.get("docchat_model")
        model = model.strip() if isinstance(model, str) else ""
        if not provider:
            if model:
                raise AISettingsValidationError("Informe o provider do DocChat antes de definir o modelo.")
            return {"docchat_provider": None, "docchat_model": None}
        # Só integrações externas já funcionais são elegíveis. O provider
        # nativo não é um destino futuro de DocChat nesta versão.
        if provider not in {"gemini", "openai"}:
            raise AISettingsValidationError("O provider selecionado não é elegível para o DocChat futuro.")
        if not model:
            raise AISettingsValidationError("Informe o modelo do DocChat quando um provider for selecionado.")
        if len(model) > 160:
            raise AISettingsValidationError("O nome do modelo do DocChat pode ter no máximo 160 caracteres.")
        return {"docchat_provider": provider, "docchat_model": model}

    def save_configuration(self, raw: Mapping[str, object]) -> AssistantAISettings:
        values = self.normalize_configuration(raw)
        settings = self.get_settings()
        for field, value in values.items():
            setattr(settings, field, value)
        # O chamador registra a auditoria e faz um único commit atômico.
        return settings

    def save_docchat_configuration(self, raw: Mapping[str, object]) -> AssistantAISettings:
        values = self.normalize_docchat_configuration(raw)
        settings = self.get_settings()
        settings.docchat_provider = values["docchat_provider"]
        settings.docchat_model = values["docchat_model"]
        return settings

    def get_docchat_ai_config(self) -> dict[str, str | None]:
        """Retorna somente a configuração persistida; não instancia provider."""
        settings = self.get_settings()
        provider = settings.docchat_provider
        model = settings.docchat_model
        return {
            "provider": provider,
            "model": model,
            "status": "configured" if provider and model else "not_configured",
        }

    @staticmethod
    def connection_status_label(state: object) -> str:
        """Converte somente estados fechados em texto seguro para o painel."""
        return CONNECTION_STATUS_LABELS.get(state, CONNECTION_STATUS_LABELS[CONNECTION_NOT_TESTED])

    def connection_state(self, provider_id: str) -> str:
        """Lê o último teste persistido de um provider sem guardar segredos.

        ``AuditLog`` já é a trilha persistida para ações administrativas. Usá-la
        evita criar coluna ou migration somente para um diagnóstico operacional;
        o estado permanece após um reload da página e não mistura providers.
        """
        spec = PROVIDERS.get(provider_id, {})
        event = db.session.scalars(
            db.select(AuditLog)
            .where(
                AuditLog.action == CONNECTION_AUDIT_ACTION,
                AuditLog.target_type == "assistant_ai_provider",
                AuditLog.target_id == provider_id,
            )
            .order_by(AuditLog.timestamp.desc())
            .limit(1)
        ).first()
        recorded = event.after_json.get("state") if event is not None and isinstance(event.after_json, Mapping) else None
        if recorded in CONNECTION_STATUS_LABELS:
            return recorded
        # O motor local não depende de rede ou credenciais: sua disponibilidade
        # é conhecida pela própria integração instalada, antes de um teste manual.
        if spec.get("native") and spec.get("implemented"):
            return CONNECTION_NATIVE_AVAILABLE
        return CONNECTION_NOT_TESTED

    def selection_metadata(self) -> dict[str, object]:
        """Metadados operacionais sem prompt, corpus ou credencial para logs."""
        settings = self.get_settings()
        return {
            "strategy": settings.strategy,
            "primary_provider": settings.primary_provider,
            "fallback_order": tuple(settings.fallback_order or ()),
        }

    def provider_statuses(self, settings: AssistantAISettings | None = None) -> list[dict[str, Any]]:
        settings = settings or self.get_settings()
        enabled = set(_clean_provider_list(settings.enabled_providers))
        result = []
        for provider in PROVIDER_ORDER:
            spec = PROVIDERS[provider]
            credential_key = spec["credential_config"]
            credential = self._config.get(credential_key, "") if credential_key else None
            connection_state = self.connection_state(provider)
            result.append({
                "id": provider,
                "display_name": spec["display_name"],
                "implemented": spec["implemented"],
                "credential_configured": None if spec.get("native") else bool(isinstance(credential, str) and credential.strip()),
                "credential_required": not spec.get("native", False),
                "provider_type": "native" if spec.get("native") else "external",
                "capabilities": list(spec.get("capabilities", ())),
                "enabled": provider in enabled and spec["implemented"],
                "is_primary": provider == settings.primary_provider,
                "selected_model": getattr(settings, spec["model_field"]),
                "connection_state": connection_state,
                "connection_status": self.connection_status_label(connection_state),
            })
        return result

    def provider_ids_for_turn(self) -> list[str]:
        settings = self.get_settings()
        values = self.normalize_configuration({
            "strategy": settings.strategy,
            "primary_provider": settings.primary_provider,
            "enabled_providers": settings.enabled_providers,
            "fallback_order": settings.fallback_order,
            "gemini_model": settings.gemini_model,
            "openai_model": settings.openai_model,
            "anthropic_model": settings.anthropic_model,
            "analysis_native_model": getattr(settings, "analysis_native_model", DEFAULT_ANALYSIS_NATIVE_MODEL),
        })
        return [values["primary_provider"], *values["fallback_order"]]

    def build_provider(self, provider_id: str, settings: AssistantAISettings | None = None) -> AIProvider:
        settings = settings or self.get_settings()
        if provider_id == "analysis_native":
            return AnalysisNativeProvider(model=getattr(settings, "analysis_native_model", DEFAULT_ANALYSIS_NATIVE_MODEL))
        if provider_id == "gemini":
            return GeminiProvider(api_key=self._config.get("GEMINI_API_KEY"), model=settings.gemini_model)
        if provider_id == "openai":
            return OpenAIProvider(api_key=self._config.get("OPENAI_API_KEY"), model=settings.openai_model)
        raise AIProviderError("O provider selecionado ainda não está disponível.", reason_class="not_implemented")

    def provider_attempt_ids(self) -> list[str]:
        injected = (self.app.extensions if self.app is not None else current_app.extensions).get("assistant_ai_provider")
        if injected is not None:
            # Mantém o contrato de testes da Fase 4.2 sem transformar o mock em
            # configuração operacional persistida.
            return ["__injected__"]
        return self.provider_ids_for_turn()

    def provider_for_attempt(self, provider_id: str) -> AIProvider:
        extensions = self.app.extensions if self.app is not None else current_app.extensions
        if provider_id == "__injected__":
            injected = extensions.get("assistant_ai_provider")
            if injected is not None:
                return injected
        return self.build_provider(provider_id)

    def test_provider_connection(self, provider_id: str) -> None:
        if provider_id not in PROVIDERS or not PROVIDERS[provider_id]["implemented"]:
            raise AISettingsValidationError("Este provider ainda não possui integração disponível.")
        self.build_provider(provider_id).test_connection()


__all__ = [
    "ACTIVE_STRATEGIES", "AIProviderManager", "AISettingsValidationError", "CONNECTION_AUDIT_ACTION",
    "CONNECTION_AVAILABLE", "CONNECTION_FAILED", "CONNECTION_NATIVE_AVAILABLE", "CONNECTION_NOT_TESTED",
    "CONNECTION_STATUS_LABELS", "FUTURE_ROUTING_TASKS", "PROVIDERS", "PROVIDER_ORDER", "STRATEGIES",
    "default_settings_values",
]
