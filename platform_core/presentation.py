"""Rótulos visíveis; códigos persistidos permanecem estáveis."""

from __future__ import annotations

from datetime import datetime


EDUCATION = {
    "elementary": "Ensino fundamental", "high_school": "Ensino médio",
    "undergraduate_in_progress": "Graduação em andamento", "undergraduate": "Graduação completa",
    "specialization": "Especialização", "masters_in_progress": "Mestrado em andamento",
    "masters": "Mestrado", "doctorate_in_progress": "Doutorado em andamento",
    "doctorate": "Doutorado", "postdoctorate": "Pós-doutorado",
    "other": "Outro", "prefer_not_to_say": "Prefiro não declarar",
}
GENDER = {"male": "Masculino", "female": "Feminino", "prefer_not_to_say": "Prefiro não declarar"}
RACE_COLOR = {
    "white": "Branca", "black": "Preta", "brown": "Parda", "yellow": "Amarela",
    "indigenous": "Indígena", "prefer_not_to_say": "Prefiro não declarar",
}
LABELS = {
    "active": "Ativo", "suspended": "Suspenso", "blocked": "Bloqueado",
    "trial": "Período de teste", "payment_pending": "Pagamento pendente",
    "expired": "Expirado", "canceled": "Cancelado", "archived": "Arquivado",
    "deleted": "Excluído", "draft": "Rascunho", "published": "Publicada",
    "inactive": "Inativa", "admin": "Administrador", "user": "Usuário",
    "paid": "Assinatura paga", "student_free": "Gratuito — Estudante",
    "admin_courtesy": "Cortesia administrativa", "inherit": "Herdar do plano",
    "allow": "Permitir", "deny": "Bloquear", "student": "Estudante",
    "researcher": "Pesquisador", "pro": "Pro", "institutional": "Institucional",
    "pdf_scraper": "Raspagem padrão", "document_analysis": "Análise documental",
    "user_registered": "Usuário cadastrado", "admin_created": "Administrador criado",
    "user_status_changed": "Estado do usuário alterado",
    "access_grant_changed": "Acesso alterado", "tool_override_changed": "Permissão de ferramenta alterada",
    "user_soft_deleted": "Usuário excluído logicamente", "user_restored": "Usuário restaurado", "tool_state_changed": "Estado da ferramenta alterado",
    "project_status_changed": "Estado do projeto alterado",
    "project_archived": "Projeto arquivado", "project_restored": "Projeto desarquivado",
    "user_password_reset": "Senha de usuário redefinida", "password_changed_after_reset": "Senha alterada após redefinição",
    "plan_state_changed": "Estado do plano alterado",
    "project_permanently_deleted": "Projeto excluído permanentemente",
    "library_state_changed": "Estado da biblioteca alterado",
    "library_created": "Biblioteca criada", "library_draft_changed": "Rascunho da biblioteca alterado",
    "library_published": "Biblioteca publicada", "user_permanently_deleted": "Usuário excluído permanentemente", "name": "Nome", "owner_user_id": "Proprietário",
    "hash": "Hash", "action": "Ação",
    "group_added": "Grupo adicionado", "entity_added": "Entidade adicionada", "variant_added": "Variante adicionada",
    "item_state_changed": "Estado do item alterado",
    "user": "Usuário", "tool": "Ferramenta", "project": "Projeto", "library": "Biblioteca",
    "role": "Papel", "status": "Estado", "plan": "Plano", "mode": "Forma de acesso",
    "decision": "Permissão", "expires_at": "Validade", "deleted": "Excluído",
    "true": "Sim", "false": "Não",
    "autor": "Autor", "movimento": "Movimento", "conceito": "Conceito",
    "obra": "Obra", "categoria_racial": "Categoria racial", "pais_regiao": "País/região",
    "tradicao": "Tradição", "termo_contextual": "Termo contextual", "outro": "Outro",
    "afro_estadunidense": "Afro-estadunidense", "negritude": "Négritude",
    "anticolonial": "Anticolonial", "pan_africana": "Pan-africana",
    "lutas_liberacao_africana": "Lutas de libertação africana", "marxista": "Marxista",
    "intelectual_brasileira": "Intelectual brasileira", "outra": "Outra",
    "indeterminada": "Indeterminada",
}


def label(value: object) -> str:
    if value is None or value == "":
        return "Não informado"
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    return LABELS.get(str(value), EDUCATION.get(str(value), GENDER.get(str(value), RACE_COLOR.get(str(value), str(value)))))


def audit_details(value: dict | None) -> str:
    if not value:
        return "—"
    parts = []
    for key, item in value.items():
        if isinstance(item, datetime):
            item = item.strftime("%d/%m/%Y %H:%M")
        parts.append(f"{label(key)}: {label(item)}")
    return "; ".join(parts)


def date_br(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    try:
        return datetime.fromisoformat(str(value)).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def register_presentation(app) -> None:
    app.jinja_env.filters["rotulo"] = label
    app.jinja_env.filters["detalhes_auditoria"] = audit_details
    app.jinja_env.filters["data_br"] = date_br
    app.jinja_env.globals.update(education_options=EDUCATION, gender_options=GENDER, race_options=RACE_COLOR, presentation_labels=LABELS)
