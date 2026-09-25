"""Identidades dos tipos de projeto; IDs de ferramentas permanecem estáveis."""

FREE = "free"
SYSTEMATIC = "systematic"
QUALITATIVE = "qualitative"
QUALITATIVE_TOOL = "qualitative_analysis"
TOOL_BY_TYPE = {
    FREE: "pdf_scraper",
    SYSTEMATIC: "document_analysis",
    QUALITATIVE: QUALITATIVE_TOOL,
}
LABEL_BY_TYPE = {
    FREE: "Análise por termos",
    SYSTEMATIC: "Análise estruturada",
    QUALITATIVE: "Análise qualitativa",
}
LABEL_BY_TOOL = {tool: LABEL_BY_TYPE[kind] for kind, tool in TOOL_BY_TYPE.items()}


def tool_for_project(project) -> str:
    return TOOL_BY_TYPE[project.scrape_type]
