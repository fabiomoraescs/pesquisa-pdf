"""Identidades públicas das duas raspagens; IDs de ferramentas permanecem estáveis."""

FREE = "free"
SYSTEMATIC = "systematic"
TOOL_BY_TYPE = {FREE: "pdf_scraper", SYSTEMATIC: "document_analysis"}
LABEL_BY_TYPE = {FREE: "Raspagem livre", SYSTEMATIC: "Raspagem sistemática"}
LABEL_BY_TOOL = {tool: LABEL_BY_TYPE[kind] for kind, tool in TOOL_BY_TYPE.items()}


def tool_for_project(project) -> str:
    return TOOL_BY_TYPE[project.scrape_type]
