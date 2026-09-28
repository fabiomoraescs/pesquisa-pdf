"""Paleta controlada dos códigos; a cor persistida é apenas o identificador."""

QUALITATIVE_CODE_PALETTE = {
    "blue": ("Azul", "#93C5FD"),
    "yellow": ("Amarelo", "#FDE68A"),
    "green": ("Verde", "#86D8A8"),
    "red": ("Vermelho", "#FCA5A5"),
    "orange": ("Laranja", "#FDBA8C"),
    "purple": ("Roxo", "#C4B5FD"),
    "pink": ("Rosa", "#F9A8D4"),
    "turquoise": ("Turquesa", "#7DD3C7"),
    "gray": ("Cinza", "#CBD5E1"),
}
DEFAULT_CODE_COLOR = "blue"
LEGACY_CODE_COLORS = {
    "#2563EB": "blue",
    "#FACC15": "yellow",
    "#16A34A": "green",
    "#DC2626": "red",
    "#EA580C": "orange",
    "#7C3AED": "purple",
    "#DB2777": "pink",
    "#0D9488": "turquoise",
    "#64748B": "gray",
}


def canonical_code_color(color: str | None) -> str:
    """Converte HEXs legados explicitamente reconhecidos no ID da paleta atual."""
    if color in QUALITATIVE_CODE_PALETTE:
        return color
    if isinstance(color, str):
        return LEGACY_CODE_COLORS.get(color.upper(), DEFAULT_CODE_COLOR)
    return DEFAULT_CODE_COLOR


def code_color_style(color: str | None) -> dict[str, str]:
    """Resolve legado NULL e deriva foreground acessível da única cor base."""
    color = canonical_code_color(color)
    hex_color = QUALITATIVE_CODE_PALETTE[color][1]
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    luminance = sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))
    return {"color": color, "color_hex": hex_color,
            "color_text": "#000000" if luminance > 0.179 else "#FFFFFF"}


def code_palette_options() -> list[dict[str, str]]:
    return [{"id": identifier, "label": label, **code_color_style(identifier)}
            for identifier, (label, _) in QUALITATIVE_CODE_PALETTE.items()]
