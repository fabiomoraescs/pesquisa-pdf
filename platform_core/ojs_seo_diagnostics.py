"""Diagnóstico bibliográfico OAI-PMH/Dublin Core, sem consultas externas.

O estado atual em :class:`OjsArticle` é útil para o ciclo de vida do
periódico, mas não é evidência suficiente para explicar uma auditoria passada.
Este módulo sempre ancora uma consulta em uma ``OjsArticleObservation`` de uma
única auditoria concluída (ou, apenas quando não houver concluída, parcial).
Assim, uma auditoria pendente ou uma coleta posterior jamais altera um
diagnóstico histórico exibido ao usuário.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from math import ceil
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import String, and_, case, cast, false, func, or_, select

from .extensions import db
from .models import OjsArticle, OjsArticleObservation, OjsJournal, OjsSeoAuditRun, User
from .ojs_oai_client import DC_FIELDS
from .ojs_seo_services import OjsSeoNotFoundError, get_ojs_journal
from .pagination import PAGE_SIZE, PlatformPage


DiagnosticStatus = Literal["present", "absent", "unverified"]
ReferenceKind = Literal["completed", "partial"]

# A ordem é a mesma do relatório agregado e da interface OJS SEO.  O cliente
# normaliza campos em listas, mas o diagnóstico também suporta evidências
# antigas ou importadas que não tragam uma chave de maneira verificável.
DIAGNOSTIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "Título"),
    ("creator", "Autoria"),
    ("description", "Descrição"),
    ("subject", "Assunto"),
    ("language", "Idioma"),
    ("date", "Data"),
    ("type", "Tipo"),
    ("identifier", "Identificadores"),
)
_DIAGNOSTIC_FIELD_CODES = tuple(field for field, _label in DIAGNOSTIC_FIELDS)
assert set(_DIAGNOSTIC_FIELD_CODES) == set(DC_FIELDS)

FILTER_LABELS: dict[str, str] = {
    "all": "Todos os registros ativos",
    "missing_title": "Sem título",
    "missing_creator": "Sem autoria",
    "missing_description": "Sem descrição",
    "missing_subject": "Sem assunto",
    "missing_language": "Sem idioma",
    "missing_date": "Sem data",
    "missing_type": "Sem tipo",
    "missing_identifier": "Sem identificador",
    "missing_doi": "Sem DOI identificado",
    "has_gaps": "Com alguma lacuna Dublin Core",
    "complete": "Sem lacunas Dublin Core",
}


class OjsSeoDiagnosticFilterError(ValueError):
    """Filtro de diagnóstico fora da lista explícita de opções permitidas."""


@dataclass(frozen=True)
class OjsDcFieldDiagnostic:
    code: str
    label: str
    status: DiagnosticStatus
    values: tuple[str, ...]


@dataclass(frozen=True)
class OjsDiagnosticReference:
    journal: OjsJournal
    audit_run: OjsSeoAuditRun
    kind: ReferenceKind


@dataclass(frozen=True)
class OjsArticleDiagnostic:
    article: OjsArticle
    observation: OjsArticleObservation
    reference: OjsDiagnosticReference
    fields: tuple[OjsDcFieldDiagnostic, ...]
    doi_status: Literal["identified", "not_identified", "no_identifier"]
    public_urls: tuple[str, ...]

    @property
    def has_gaps(self) -> bool:
        return any(field.status == "absent" for field in self.fields) or self.doi_status != "identified"

    @property
    def missing_labels(self) -> tuple[str, ...]:
        labels = [field.label for field in self.fields if field.status == "absent"]
        if self.doi_status != "identified":
            labels.append("DOI")
        return tuple(labels)


@dataclass(frozen=True)
class OjsDiagnosticSummary:
    active_records: int
    missing_description: int
    missing_subject: int
    missing_doi: int
    with_gaps: int
    without_gaps: int


@dataclass(frozen=True)
class OjsArticleDiagnosticListing:
    journal: OjsJournal
    reference: OjsDiagnosticReference | None
    articles: PlatformPage[OjsArticleDiagnostic]
    summary: OjsDiagnosticSummary | None
    selected_filter: str
    search: str
    record_state: Literal["active", "deleted"]


def _normalized_search(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Limita uma entrada de consulta sem modificar nem registrar seus valores.
    return " ".join(value.split())[:200]


def _validate_filter(value: object) -> str:
    candidate = value if isinstance(value, str) else "all"
    if candidate not in FILTER_LABELS:
        raise OjsSeoDiagnosticFilterError("Filtro de diagnóstico inválido.")
    return candidate


def _validate_record_state(value: object) -> Literal["active", "deleted"]:
    if value in {None, "", "active"}:
        return "active"
    if value == "deleted":
        return "deleted"
    raise OjsSeoDiagnosticFilterError("Estado de registro inválido.")


def _ordered_reference_statement(journal_id: str, status: str):
    # ``completed_at`` pode ser nulo em dados legados. O primeiro critério torna
    # a ordenação idêntica em SQLite e PostgreSQL antes do desempate por criação.
    return (
        select(OjsSeoAuditRun)
        .where(OjsSeoAuditRun.journal_id == journal_id, OjsSeoAuditRun.status == status)
        .order_by(
            OjsSeoAuditRun.completed_at.is_(None).asc(),
            OjsSeoAuditRun.completed_at.desc(),
            OjsSeoAuditRun.created_at.desc(),
            OjsSeoAuditRun.id.desc(),
        )
    )


def _resolve_reference_for_authorized_journal(
    journal: OjsJournal, audit_run_id: str | None = None,
) -> OjsDiagnosticReference | None:
    if audit_run_id:
        if len(audit_run_id) > 64:
            raise OjsSeoNotFoundError("ojs_audit_not_found", "A auditoria OJS não foi encontrada.")
        run = db.session.scalar(
            select(OjsSeoAuditRun).where(
                OjsSeoAuditRun.id == audit_run_id,
                OjsSeoAuditRun.journal_id == journal.id,
                OjsSeoAuditRun.status.in_(("completed", "partial")),
            )
        )
        if run is None:
            raise OjsSeoNotFoundError("ojs_audit_not_found", "A auditoria OJS não foi encontrada.")
        return OjsDiagnosticReference(journal, run, run.status)

    completed = db.session.scalar(_ordered_reference_statement(journal.id, "completed").limit(1))
    if completed is not None:
        return OjsDiagnosticReference(journal, completed, "completed")
    partial = db.session.scalar(_ordered_reference_statement(journal.id, "partial").limit(1))
    if partial is not None:
        return OjsDiagnosticReference(journal, partial, "partial")
    return None


def resolve_ojs_diagnostic_reference(
    actor: User, journal_id: str, *, audit_run_id: str | None = None, include_inactive: bool = False,
) -> OjsDiagnosticReference | None:
    """Obtém a coleta que fundamenta o diagnóstico após validar ownership.

    ``pending``, ``running``, ``failed`` e ``canceled`` nunca são fontes de
    indicadores, mesmo que sejam as execuções solicitadas mais recentemente.
    """
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return _resolve_reference_for_authorized_journal(journal, audit_run_id)


def _json_array_conditions(field: str):
    """Retorna condições SQL portáveis de presença/ausência de uma lista DC.

    A persistência normaliza campos DC em listas. Ainda assim, uma chave ausente
    ou um valor que não seja lista é *não verificado*, portanto não aparece nos
    filtros de ausência nem infla os indicadores de lacunas.
    """
    dialect = db.session.get_bind().dialect.name
    payload = OjsArticleObservation.dublin_core_json
    if dialect == "postgresql":
        value = payload.op("->")(field)
        array = func.jsonb_typeof(value) == "array"
        length = func.jsonb_array_length(value)
    elif dialect == "sqlite":
        path = f"$.{field}"
        array = func.json_type(payload, path) == "array"
        length = func.json_array_length(payload, path)
    else:
        # SQLAlchemy JSON não fornece uma função de tamanho de arrays comum a
        # todos os bancos suportados. Falhar de forma explícita é mais seguro do
        # que classificar incorretamente metadados em um dialeto não homologado.
        raise RuntimeError(f"Dialeto não suportado para diagnóstico JSON: {dialect}")
    return and_(array, length > 0), and_(array, length == 0)


def _query_conditions(selected_filter: str):
    present = {}
    absent = {}
    for field in _DIAGNOSTIC_FIELD_CODES:
        present[field], absent[field] = _json_array_conditions(field)
    missing_doi = or_(
        OjsArticleObservation.doi_observed.is_(None),
        func.length(func.trim(OjsArticleObservation.doi_observed)) == 0,
    )
    if selected_filter == "all":
        return present, absent, None
    if selected_filter.startswith("missing_"):
        code = selected_filter.removeprefix("missing_")
        condition = missing_doi if code == "doi" else absent[code]
        return present, absent, condition
    if selected_filter == "has_gaps":
        return present, absent, or_(*absent.values())
    if selected_filter == "complete":
        return present, absent, and_(*present.values())
    raise AssertionError("Filtro validado não foi tratado.")


def _base_observation_statement(reference: OjsDiagnosticReference, record_state: str, search: str):
    statement = (
        select(OjsArticle, OjsArticleObservation)
        .join(OjsArticleObservation, OjsArticleObservation.article_id == OjsArticle.id)
        .where(
            OjsArticle.journal_id == reference.journal.id,
            OjsArticleObservation.journal_id == reference.journal.id,
            OjsArticleObservation.audit_run_id == reference.audit_run.id,
            OjsArticleObservation.record_status == record_state,
        )
    )
    if search:
        # O texto JSON inclui títulos, autoria, DOI e identificadores. A busca
        # permanece no banco e não transforma identificadores externos em SQL.
        pattern = f"%{search}%"
        statement = statement.where(or_(
            OjsArticle.oai_identifier.ilike(pattern),
            OjsArticleObservation.doi_observed.ilike(pattern),
            cast(OjsArticleObservation.dublin_core_json, String).ilike(pattern),
        ))
    return statement


def _safe_public_urls(values: tuple[str, ...]) -> tuple[str, ...]:
    """Mantém somente URLs HTTPS públicas sintaticamente seguras para links.

    Não há acesso de rede nesta etapa. Isso evita transformar identificadores
    maliciosos ou com credenciais em ``href`` navegáveis na interface.
    """
    result: list[str] = []
    for value in values:
        if len(value) > 2_048 or any(ord(char) < 32 for char in value):
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        host = parsed.hostname.lower() if parsed.hostname else ""
        try:
            literal_address_is_public = ip_address(host).is_global
        except ValueError:
            literal_address_is_public = not (host == "localhost" or host.endswith(".localhost"))
        if (
            parsed.scheme.lower() == "https"
            and host
            and parsed.username is None
            and parsed.password is None
            and literal_address_is_public
            and value not in result
        ):
            result.append(value)
    return tuple(result)


def _field_diagnostics(observation: OjsArticleObservation) -> tuple[OjsDcFieldDiagnostic, ...]:
    payload = observation.dublin_core_json if isinstance(observation.dublin_core_json, dict) else {}
    diagnostics: list[OjsDcFieldDiagnostic] = []
    for code, label in DIAGNOSTIC_FIELDS:
        raw_values = payload.get(code)
        if not isinstance(raw_values, list):
            diagnostics.append(OjsDcFieldDiagnostic(code, label, "unverified", ()))
            continue
        values = tuple(
            value.strip() for value in raw_values if isinstance(value, str) and value.strip()
        )
        diagnostics.append(OjsDcFieldDiagnostic(
            code, label, "present" if values else "absent", values,
        ))
    return tuple(diagnostics)


def _diagnostic_from_row(
    article: OjsArticle, observation: OjsArticleObservation, reference: OjsDiagnosticReference,
) -> OjsArticleDiagnostic:
    fields = _field_diagnostics(observation)
    identifier = next(field for field in fields if field.code == "identifier")
    doi_status: Literal["identified", "not_identified", "no_identifier"]
    if isinstance(observation.doi_observed, str) and observation.doi_observed.strip():
        doi_status = "identified"
    elif identifier.status == "absent":
        doi_status = "no_identifier"
    else:
        doi_status = "not_identified"
    return OjsArticleDiagnostic(
        article=article,
        observation=observation,
        reference=reference,
        fields=fields,
        doi_status=doi_status,
        public_urls=_safe_public_urls(identifier.values),
    )


def _summary_for_reference(reference: OjsDiagnosticReference) -> OjsDiagnosticSummary:
    present, absent, _ = _query_conditions("all")
    base = and_(
        OjsArticleObservation.journal_id == reference.journal.id,
        OjsArticleObservation.audit_run_id == reference.audit_run.id,
        OjsArticleObservation.record_status == "active",
    )
    any_gap = or_(*absent.values())
    no_gaps = and_(*present.values())

    def counted(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    row = db.session.execute(select(
        func.count(OjsArticleObservation.id),
        counted(absent["description"]),
        counted(absent["subject"]),
        counted(or_(
            OjsArticleObservation.doi_observed.is_(None),
            func.length(func.trim(OjsArticleObservation.doi_observed)) == 0,
        )),
        counted(any_gap),
        counted(no_gaps),
    ).where(base)).one()
    return OjsDiagnosticSummary(*(int(value or 0) for value in row))


def list_ojs_article_diagnostics(
    actor: User,
    journal_id: str,
    *,
    page: int = 1,
    selected_filter: object = "all",
    search: object = "",
    record_state: object = "active",
    audit_run_id: str | None = None,
    include_inactive: bool = False,
) -> OjsArticleDiagnosticListing:
    """Lista evidências de uma única auditoria com paginação no banco.

    A listagem é deliberadamente baseada em observações, não no estado atual do
    artigo. Isto mantém indicadores, filtros e página individual vinculados à
    mesma coleta que o usuário escolheu consultar.
    """
    selected = _validate_filter(selected_filter)
    state = _validate_record_state(record_state)
    query = _normalized_search(search)
    reference = resolve_ojs_diagnostic_reference(
        actor, journal_id, audit_run_id=audit_run_id, include_inactive=include_inactive,
    )
    if reference is None:
        journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
        return OjsArticleDiagnosticListing(
            journal, None, PlatformPage((), 0, 1), None, selected, query, state,
        )

    _present, _absent, filter_condition = _query_conditions(selected)
    base = _base_observation_statement(reference, state, query)
    if state == "deleted" and selected != "all":
        # Tombstones OAI-PMH carregam listas DC vazias por normalização. Elas
        # não são evidência de uma lacuna editorial; portanto, a combinação é
        # válida, mas não classifica nem devolve registros excluídos como
        # "sem descrição", "sem DOI" etc. O estado vazio preserva a distinção
        # histórica sem transformar uma preferência de filtro em HTTP 400.
        base = base.where(false())
    elif filter_condition is not None:
        base = base.where(filter_condition)
    total = int(db.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    total_pages = max(1, ceil(total / PAGE_SIZE))
    current_page = min(max(1, page), total_pages)
    rows = db.session.execute(
        base.order_by(OjsArticle.oai_identifier.asc(), OjsArticle.id.asc())
        .limit(PAGE_SIZE)
        .offset((current_page - 1) * PAGE_SIZE)
    ).all()
    diagnostics = tuple(_diagnostic_from_row(article, observation, reference) for article, observation in rows)
    return OjsArticleDiagnosticListing(
        reference.journal,
        reference,
        PlatformPage(diagnostics, total, current_page),
        _summary_for_reference(reference),
        selected,
        query,
        state,
    )


def list_all_ojs_article_diagnostics(
    actor: User,
    journal_id: str,
    *,
    selected_filter: object = "all",
    search: object = "",
    record_state: object = "active",
    audit_run_id: str | None = None,
    include_inactive: bool = False,
) -> tuple[OjsDiagnosticReference | None, tuple[OjsArticleDiagnostic, ...]]:
    """Retorna todos os diagnósticos da mesma consulta usada em ``Artigos``.

    A exportação precisa do conjunto completo, e não apenas da página que está
    visível na tabela. Manter a construção do ``statement`` aqui impede que a
    exportação replique filtros Dublin Core ou passe a interpretar uma
    referência bibliográfica diferente da interface.
    """
    selected = _validate_filter(selected_filter)
    state = _validate_record_state(record_state)
    query = _normalized_search(search)
    reference = resolve_ojs_diagnostic_reference(
        actor, journal_id, audit_run_id=audit_run_id, include_inactive=include_inactive,
    )
    if reference is None:
        return None, ()

    _present, _absent, filter_condition = _query_conditions(selected)
    statement = _base_observation_statement(reference, state, query)
    if state == "deleted" and selected != "all":
        statement = statement.where(false())
    elif filter_condition is not None:
        statement = statement.where(filter_condition)
    rows = db.session.execute(
        statement.order_by(OjsArticle.oai_identifier.asc(), OjsArticle.id.asc())
    ).all()
    return reference, tuple(
        _diagnostic_from_row(article, observation, reference)
        for article, observation in rows
    )


def get_ojs_article_diagnostic(
    actor: User,
    journal_id: str,
    article_id: str,
    *,
    audit_run_id: str | None = None,
    include_inactive: bool = False,
) -> OjsArticleDiagnostic:
    """Devolve a evidência individual pertencente à auditoria de referência."""
    reference = resolve_ojs_diagnostic_reference(
        actor, journal_id, audit_run_id=audit_run_id, include_inactive=include_inactive,
    )
    if reference is None:
        raise OjsSeoNotFoundError("ojs_article_not_found", "O registro bibliográfico não foi encontrado.")
    if not isinstance(article_id, str) or not article_id or len(article_id) > 64:
        raise OjsSeoNotFoundError("ojs_article_not_found", "O registro bibliográfico não foi encontrado.")
    row = db.session.execute(
        select(OjsArticle, OjsArticleObservation)
        .join(OjsArticleObservation, OjsArticleObservation.article_id == OjsArticle.id)
        .where(
            OjsArticle.id == article_id,
            OjsArticle.journal_id == reference.journal.id,
            OjsArticleObservation.journal_id == reference.journal.id,
            OjsArticleObservation.audit_run_id == reference.audit_run.id,
        )
    ).one_or_none()
    if row is None:
        raise OjsSeoNotFoundError("ojs_article_not_found", "O registro bibliográfico não foi encontrado.")
    article, observation = row
    return _diagnostic_from_row(article, observation, reference)


def editorial_recommendations(diagnostic: OjsArticleDiagnostic) -> tuple[str, ...]:
    """Recomendações cuidadosas: descrevem apenas o que faltou no OAI-PMH."""
    recommendations = {
        "title": "Não foi identificado um título no registro Dublin Core. Verifique o preenchimento e a exposição do título do artigo no OJS.",
        "creator": "Não foi identificada autoria no registro Dublin Core. Verifique o cadastro e a exposição dos autores no OJS.",
        "description": "Não foi identificada uma descrição no registro Dublin Core. Verifique se o resumo está preenchido e corretamente exposto nos metadados do artigo no OJS.",
        "subject": "Não foram identificados assuntos no registro Dublin Core. Verifique o cadastro e a exposição das palavras-chave no OJS.",
        "language": "Não foi identificado idioma no registro Dublin Core. Verifique o preenchimento e a exposição do idioma do artigo no OJS.",
        "date": "Não foi identificada data no registro Dublin Core. Verifique o preenchimento e a exposição da data bibliográfica no OJS.",
        "type": "Não foi identificado tipo documental no registro Dublin Core. Verifique o preenchimento e a exposição do tipo no OJS.",
        "identifier": "Não foram identificados identificadores no registro Dublin Core. Verifique os identificadores expostos pelo OJS.",
    }
    result = [recommendations[field.code] for field in diagnostic.fields if field.status == "absent"]
    if diagnostic.doi_status != "identified":
        result.append(
            "Não foi possível identificar um DOI nos metadados coletados. Verifique se o artigo possui DOI e se ele está corretamente informado e disponibilizado pelo OJS."
        )
    return tuple(result)


__all__ = [
    "DIAGNOSTIC_FIELDS",
    "FILTER_LABELS",
    "OjsArticleDiagnostic",
    "OjsArticleDiagnosticListing",
    "OjsDcFieldDiagnostic",
    "OjsDiagnosticReference",
    "OjsDiagnosticSummary",
    "OjsSeoDiagnosticFilterError",
    "editorial_recommendations",
    "get_ojs_article_diagnostic",
    "list_all_ojs_article_diagnostics",
    "list_ojs_article_diagnostics",
    "resolve_ojs_diagnostic_reference",
]
