"""Limiar inicial compartilhado pelas interfaces, sem alterar o motor da V3."""

from analyzer.v3 import _configuracao

MINIMUM = 0.50
MAXIMUM = 0.90
STEP = 0.01
DEFAULT = MINIMUM


def normalize(value: object) -> float:
    """Usa os limites da V3 e o padrão exploratório para entrada inválida."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT
    return _configuracao({"limiar_semantico": value})["limiar_semantico"]


def template_settings() -> dict[str, float]:
    return {"minimum": MINIMUM, "maximum": MAXIMUM, "step": STEP, "default": DEFAULT}
