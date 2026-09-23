"""Limiar visual compartilhado com a configuração vigente da V3."""

from analyzer.v3 import LIMIAR_PADRAO, _configuracao

MINIMUM = 0.50
MAXIMUM = 0.90
STEP = 0.01
DEFAULT = LIMIAR_PADRAO


def normalize(value: object) -> float:
    """Usa exatamente o tratamento de entrada e limites da V3 existente."""
    return _configuracao({"limiar_semantico": value})["limiar_semantico"]


def template_settings() -> dict[str, float]:
    return {"minimum": MINIMUM, "maximum": MAXIMUM, "step": STEP, "default": DEFAULT}
