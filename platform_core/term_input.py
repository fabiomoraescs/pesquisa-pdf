"""Regra de separadores do campo de termos literais, compartilhada pelas ferramentas."""

MULTIPLE_SEPARATOR_MESSAGE = (
    "Para pesquisar múltiplos termos, separe cada palavra ou expressão com ponto e vírgula (;). "
    "Vírgulas e pontos não são usados como separadores de múltiplos termos."
)


def has_invalid_term_separator(text: str) -> bool:
    return "," in text or "." in text
