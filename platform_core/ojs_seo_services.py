"""Serviços internos e defensivos para cadastro futuro do OJS SEO.

Este módulo não define rotas Flask e não cria um cliente HTTP alternativo. A
descoberta delega integralmente ao cliente OAI-PMH defensivo; as consultas
sempre partem do proprietário do ``AnalyticsProject``. A autorização
operacional continua sendo a já consolidada em ``AccessGrant``, ``PlanTool``
e ``UserToolOverride``.

O cadastro comercial utiliza uma reserva durável de ``ojs_journals`` por
``WorkspaceSubscription``. Administradores com a ferramenta operacional podem
registrar periódicos próprios em um contexto administrativo explícito, sem
criar uma assinatura ou reserva sintética. A descoberta externa é concluída
antes da persistência; projeto, periódico e eventual reserva são então
confirmados em uma única transação curta. Nenhuma rota é exposta por este
módulo.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .commercial_limit_services import effective_subscription_limits
from .extensions import db
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsArticleObservation,
    OjsHtmlAuditCampaign,
    OjsHtmlAuditRun,
    OjsJournal,
    OjsSeoAnalysisOperation,
    OjsSeoAuditRun,
    PlanTool,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceSubscription,
    WorkspaceSubscriptionOjsJournal,
    utcnow,
)
from .ojs_oai_client import (
    EndpointValidationError,
    OaiClientError,
    OaiPmhAuditResult,
    OaiPmhClient,
    ValidatedOaiEndpoint,
    validate_oai_endpoint,
)
from .services import can_access_tool, record_audit


OJS_SEO_TOOL_ID = "ojs_seo"
OJS_SEO_PROJECT_MODULE = "ojs_seo"
OPEN_AUDIT_STATUSES = frozenset({"pending", "running"})


class OjsSeoServiceError(ValueError):
    """Erro seguro para uma futura camada de aplicação autenticada."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class OjsSeoAuthorizationError(OjsSeoServiceError):
    """A conta não pode consultar ou operar este domínio."""


class OjsSeoNotFoundError(OjsSeoServiceError):
    """O recurso não existe no escopo que a pessoa pode consultar."""


class OjsSeoRegistrationBlockedError(OjsSeoServiceError):
    """Compatibilidade para consumidores que tratavam o cadastro como bloqueado."""


class OjsSeoDiscoveryError(OjsSeoServiceError):
    """O endpoint não pôde ser confirmado pelo protocolo OAI-PMH."""


class OjsSeoSubscriptionError(OjsSeoServiceError):
    """A assinatura não prova direito comercial sobre a reserva OJS."""


class OjsSeoQuotaError(OjsSeoServiceError):
    """A assinatura não tem capacidade comercial para outra revista OJS."""


class OjsSeoProjectLifecycleError(OjsSeoServiceError):
    """Uma transição do ciclo de vida do projeto OJS não é segura."""


class OjsSeoProjectDeletionError(OjsSeoProjectLifecycleError):
    """A exclusão permanente do projeto OJS não satisfaz as proteções exigidas."""


class OjsSeoAuditCancellationError(OjsSeoProjectLifecycleError):
    """Uma auditoria não está mais em condição segura para cancelamento."""


ClientFactory = Callable[[str], OaiPmhClient]
EndpointValidator = Callable[[str], ValidatedOaiEndpoint]


@dataclass(frozen=True, slots=True)
class OjsJournalDiscovery:
    """Resultado mínimo e seguro de uma descoberta ainda não persistida."""

    public_url: str
    endpoint_url: str
    repository_name: str
    oai_base_url: str
    protocol_version: str
    collection_status: str


@dataclass(frozen=True, slots=True)
class OjsSeoSubscriptionUsage:
    """Uso comercial de periódicos OJS em uma assinatura acessível.

    A contagem parte exclusivamente das alocações ativas. Isso inclui um
    periódico cujo ``AnalyticsProject`` esteja arquivado, pois arquivar não
    libera a vaga comercial. A estrutura é somente de leitura e não decide se
    uma nova reserva pode ser criada; essa decisão permanece em
    ``_subscription_for_actor`` e ``_reserve_journal_in_transaction``.
    """

    subscription: WorkspaceSubscription
    used_slots: int
    contracted_limit: int
    available_slots: int


@dataclass(frozen=True, slots=True)
class OjsSeoJournalRegistrationContext:
    """Contexto explícito que autoriza um novo periódico.

    O modo administrativo não representa contrato comercial: ele é exclusivo
    de administrador ativo com a ferramenta OJS SEO operacional e nunca gera
    ``WorkspaceSubscription`` ou ``WorkspaceSubscriptionOjsJournal``.
    """

    mode: str
    subscription: WorkspaceSubscription | None = None


def require_ojs_seo_access(actor: User) -> None:
    """Aplica a autorização operacional existente sem usar ``abort``.

    Enquanto ``ojs_seo`` não possuir uma ``Tool`` operacional no catálogo,
    esta função recusa inclusive administradores. Isto impede que a simples
    existência dos modelos OJS disponibilize uma ferramenta futura.
    """
    if not isinstance(actor, User) or not actor.id or not actor.is_active:
        raise OjsSeoAuthorizationError(
            "ojs_seo_access_denied", "A conta não está autorizada a utilizar OJS SEO."
        )
    if not can_access_tool(actor, OJS_SEO_TOOL_ID):
        raise OjsSeoAuthorizationError(
            "ojs_seo_access_denied", "A conta não está autorizada a utilizar OJS SEO."
        )


def has_administrative_ojs_context(actor: User) -> bool:
    """Indica o contexto administrativo explícito, sem simular assinatura.

    A condição depende da mesma autorização operacional do módulo. Assim, uma
    conta marcada como administradora não contorna uma ferramenta desativada ou
    removida do catálogo.
    """
    return bool(
        isinstance(actor, User)
        and actor.id
        and actor.is_active
        and actor.role == "admin"
        and can_access_tool(actor, OJS_SEO_TOOL_ID)
    )


def _require_administrative_ojs_context(actor: User) -> None:
    require_ojs_seo_access(actor)
    if not has_administrative_ojs_context(actor):
        raise OjsSeoAuthorizationError(
            "ojs_seo_administrative_context_required",
            "A operação administrativa do OJS SEO não está autorizada para esta conta.",
        )


def discover_ojs_journal(
    actor: User,
    *,
    public_url: str,
    oai_endpoint_url: str,
    client_factory: ClientFactory | None = None,
    endpoint_validator: EndpointValidator = validate_oai_endpoint,
) -> OjsJournalDiscovery:
    """Confirma o endpoint fora de qualquer transação de banco.

    A URL pública é mantida como informação declarada; o endpoint canônico e
    a identidade vêm exclusivamente da validação e do ``Identify`` OAI-PMH.
    Nenhum redirect é seguido por este serviço: essa garantia é do cliente
    existente, que também fixa os endereços DNS públicos validados.
    """
    require_ojs_seo_access(actor)
    _end_read_transaction_before_network()
    normalized_public_url = _validate_declared_public_url(public_url)
    try:
        validated_endpoint = endpoint_validator(oai_endpoint_url)
        endpoint = validated_endpoint.canonical_url
        client = client_factory(endpoint) if client_factory is not None else OaiPmhClient(endpoint)
        result = client.inspect()
    except EndpointValidationError as exc:
        raise OjsSeoDiscoveryError(exc.code, exc.safe_message) from exc
    except OaiClientError as exc:
        raise OjsSeoDiscoveryError(exc.code, exc.safe_message) from exc

    return _discovery_from_result(normalized_public_url, endpoint, result)


def register_ojs_journal(
    actor: User,
    *,
    workspace_subscription_id: str | None,
    public_url: str,
    oai_endpoint_url: str | None = None,
    project_name: str,
    project_description: str = "",
    client_factory: ClientFactory | None = None,
    endpoint_validator: EndpointValidator = validate_oai_endpoint,
) -> OjsJournal:
    """Descobre e cadastra uma revista em contexto comercial ou administrativo.

    A autorização é verificada duas vezes: antes da descoberta externa e outra
    vez dentro da transação de persistência. Dessa forma, uma revogação de
    plano, membership ou ferramenta entre as duas fases não confirma um projeto
    sem direito comercial nem sem contexto administrativo válido. O serviço
    possui e confirma a transação local; a sessão precisa estar limpa antes da
    chamada.
    """
    normalized_name = _validate_project_name(project_name)
    normalized_description = _validate_project_description(project_description)
    registration_context = _registration_context_for_actor(actor, workspace_subscription_id)
    endpoint_candidate = oai_endpoint_url or suggested_oai_endpoint(public_url)
    discovery = discover_ojs_journal(
        actor,
        public_url=public_url,
        oai_endpoint_url=endpoint_candidate,
        client_factory=client_factory,
        endpoint_validator=endpoint_validator,
    )
    identity_hash = canonical_ojs_journal_identity(discovery.endpoint_url)

    try:
        with _exclusive_reservation_transaction():
            registration_context = _registration_context_for_actor(
                actor, workspace_subscription_id
            )
            subscription = registration_context.subscription
            if subscription is not None:
                _reject_active_identity_duplicate(subscription.id, identity_hash)
                _require_available_capacity(subscription)
            project = AnalyticsProject(
                owner_user_id=actor.id,
                module_key=OJS_SEO_PROJECT_MODULE,
                name=normalized_name,
                description=normalized_description,
                status="active",
            )
            journal = OjsJournal(
                analytics_project=project,
                public_url=discovery.public_url,
                oai_endpoint_url=discovery.endpoint_url,
                oai_endpoint_validated_at=utcnow(),
                repository_name=discovery.repository_name,
                last_identified_at=utcnow(),
                discovery_status="identified",
            )
            db.session.add_all((project, journal))
            db.session.flush()
            allocation = (
                _reserve_journal_in_transaction(
                    subscription, journal, identity_hash, discovery.endpoint_url
                )
                if subscription is not None
                else None
            )
            record_audit(actor, "ojs_seo_journal_registered", "ojs_journal", journal.id, {}, {
                "registration_context": registration_context.mode,
                "workspace_subscription_id": subscription.id if subscription is not None else None,
                "allocation_id": allocation.id if allocation is not None else None,
                "discovery_status": journal.discovery_status,
            })
            return journal
    except IntegrityError as exc:
        raise OjsSeoServiceError(
            "ojs_journal_allocation_conflict",
            "O periódico já possui uma reserva comercial ativa incompatível.",
        ) from exc
    except OperationalError as exc:
        raise OjsSeoServiceError(
            "ojs_journal_reservation_busy",
            "Não foi possível reservar a cota do periódico agora. Tente novamente.",
        ) from exc


def reserve_existing_ojs_journal(
    actor: User,
    *,
    workspace_subscription_id: str,
    journal_id: str,
) -> WorkspaceSubscriptionOjsJournal:
    """Reserva uma revista existente, sem permitir troca de identidade.

    É uma operação interna para migração manual futura. Não cria projetos nem
    descobre URLs; portanto não pode ser usada para pular a validação OAI-PMH
    do fluxo normal de cadastro.
    """
    _require_clean_local_session()
    try:
        with _exclusive_reservation_transaction():
            subscription = _subscription_for_actor(
                actor, workspace_subscription_id, require_capacity=True
            )
            journal = _journal_for_actor_in_transaction(actor, journal_id)
            if not journal.oai_endpoint_url or journal.discovery_status != "identified":
                raise OjsSeoServiceError(
                    "oai_endpoint_unavailable", "O periódico não possui endpoint OAI-PMH confirmado."
                )
            identity_hash = canonical_ojs_journal_identity(journal.oai_endpoint_url)
            _reject_active_identity_duplicate(subscription.id, identity_hash)
            _require_available_capacity(subscription)
            allocation = _reserve_journal_in_transaction(
                subscription, journal, identity_hash, journal.oai_endpoint_url
            )
            record_audit(actor, "ojs_seo_journal_allocated", "ojs_journal", journal.id, {}, {
                "workspace_subscription_id": subscription.id,
                "allocation_id": allocation.id,
            })
            return allocation
    except IntegrityError as exc:
        raise OjsSeoServiceError(
            "ojs_journal_allocation_conflict",
            "O periódico já possui uma reserva comercial ativa incompatível.",
        ) from exc
    except OperationalError as exc:
        raise OjsSeoServiceError(
            "ojs_journal_reservation_busy",
            "Não foi possível reservar a cota do periódico agora. Tente novamente.",
        ) from exc


def release_ojs_journal_allocation(
    actor: User, allocation_id: str
) -> WorkspaceSubscriptionOjsJournal:
    """Libera uma reserva sem apagar periódico, projetos ou auditorias.

    A liberação permanece disponível ao titular/admin mesmo após expiração da
    assinatura, pois é uma operação de histórico e não concede nova capacidade.
    """
    _require_clean_local_session()
    with _exclusive_reservation_transaction():
        allocation = db.session.get(WorkspaceSubscriptionOjsJournal, allocation_id)
        if allocation is None:
            raise OjsSeoNotFoundError("ojs_allocation_not_found", "A alocação OJS não foi encontrada.")
        _subscription_for_actor(
            actor, allocation.workspace_subscription_id,
            require_capacity=False,
            require_operational_access=False,
            require_active_subscription=False,
        )
        if allocation.status != "active":
            raise OjsSeoServiceError("ojs_allocation_not_active", "A alocação OJS já foi liberada.")
        allocation.status = "released"
        allocation.released_at = utcnow()
        db.session.flush()
        record_audit(actor, "ojs_seo_journal_allocation_released", "ojs_journal", allocation.ojs_journal_id, {
            "allocation_id": allocation.id, "status": "active",
        }, {"status": "released"})
        return allocation


def delete_ojs_seo_project_permanently(
    actor: User, journal_id: str, confirmation: str,
) -> None:
    """Remove um projeto OJS SEO e os dados que só existem por causa dele.

    A confirmação segue o padrão permanente da plataforma. A operação começa
    em uma transação de escrita curta e serializada: no SQLite isso impede que
    outro processo reivindique uma auditoria entre a verificação e a remoção;
    no PostgreSQL as linhas do periódico, das auditorias e das reservas são
    bloqueadas. Auditorias pendentes ou em execução impedem a exclusão, pois
    um worker ainda poderia estar usando o endpoint ou prestes a persistir
    resultados.

    Não há arquivos privados do OJS SEO nesta fase. As observações, artigos,
    auditorias e reservas são removidos explicitamente na ordem das FKs. A
    reserva ativa é primeiro marcada como liberada e auditada; como o projeto
    é uma exclusão definitiva, a linha histórica da reserva também precisa ser
    removida para não manter uma FK restritiva para um periódico inexistente.
    O ``AuditLog`` preserva a evidência da liberação e da exclusão.

    O serviço possui a transação e confirma o commit somente depois que toda a
    cadeia foi removida. Qualquer exceção desfaz integralmente a operação.
    """
    if confirmation.strip() != "deletar":
        raise OjsSeoProjectDeletionError(
            "ojs_project_delete_confirmation_required",
            'Digite exatamente "deletar" para confirmar a exclusão.',
        )
    require_ojs_seo_access(actor)
    _require_clean_local_session()

    try:
        with _exclusive_reservation_transaction():
            journal = _locked_journal_for_actor_in_transaction(actor, journal_id)
            project = journal.analytics_project
            if project is None or project.module_key != OJS_SEO_PROJECT_MODULE:
                raise OjsSeoNotFoundError(
                    "ojs_journal_not_found", "O periódico OJS não foi encontrado."
                )
            if project.status != "archived" or project.archived_at is None:
                raise OjsSeoProjectDeletionError(
                    "ojs_project_not_archived",
                    "Somente periódicos arquivados podem ser excluídos permanentemente.",
                )

            # ``AnalyticsRun`` pertence ao núcleo Analytics e não faz parte do
            # lifecycle OJS. Uma referência inesperada é tratada como falha
            # segura, em vez de apagar dados de outro domínio por inferência.
            unrelated_run = db.session.scalar(
                select(AnalyticsRun.id)
                .where(AnalyticsRun.analytics_project_id == project.id)
                .limit(1)
            )
            if unrelated_run is not None:
                raise OjsSeoProjectDeletionError(
                    "ojs_project_unexpected_analytics_data",
                    "O periódico contém dados analíticos não pertencentes ao OJS SEO e não pode ser excluído automaticamente.",
                )

            open_run = db.session.scalar(
                _locked_statement(
                    select(OjsSeoAuditRun.id).where(
                        OjsSeoAuditRun.journal_id == journal.id,
                        OjsSeoAuditRun.status.in_(OPEN_AUDIT_STATUSES),
                    )
                ).limit(1)
            )
            open_html_run = db.session.scalar(
                _locked_statement(
                    select(OjsHtmlAuditRun.id).where(
                        OjsHtmlAuditRun.journal_id == journal.id,
                        OjsHtmlAuditRun.status.in_(OPEN_AUDIT_STATUSES),
                    )
                ).limit(1)
            )
            if open_run is not None or open_html_run is not None:
                raise OjsSeoProjectDeletionError(
                    "ojs_project_audit_open",
                    "Não é possível excluir o periódico enquanto houver uma auditoria pendente ou em execução.",
                )

            allocations = list(db.session.scalars(
                _locked_statement(
                    select(WorkspaceSubscriptionOjsJournal).where(
                        WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id,
                    )
                )
            ))
            if not allocations:
                _require_administrative_journal_context(actor, journal)
            released_allocations = 0
            for allocation in allocations:
                # A mesma checagem usada para liberação isolada prova que o
                # ator ainda pode administrar a assinatura responsável.
                _subscription_for_actor(
                    actor,
                    allocation.workspace_subscription_id,
                    require_capacity=False,
                    require_operational_access=False,
                    require_active_subscription=False,
                )
                if allocation.status == "active":
                    allocation.status = "released"
                    allocation.released_at = utcnow()
                    released_allocations += 1
                    record_audit(
                        actor,
                        "ojs_seo_journal_allocation_released",
                        "ojs_journal",
                        journal.id,
                        {"allocation_id": allocation.id, "status": "active"},
                        {"status": "released", "reason": "project_permanently_deleted"},
                    )
            db.session.flush()

            _delete_ojs_project_records(journal, project)
            record_audit(
                actor,
                "ojs_seo_project_permanently_deleted",
                "analytics_project",
                project.id,
                {
                    "owner_user_id": project.owner_user_id,
                    "module_key": project.module_key,
                    "name": project.name,
                    "journal_id": journal.id,
                },
                {
                    "deleted": True,
                    "released_allocations": released_allocations,
                },
            )
    except OperationalError as exc:
        raise OjsSeoProjectDeletionError(
            "ojs_project_delete_busy",
            "Não foi possível excluir o periódico agora porque há outra operação em andamento. Tente novamente.",
        ) from exc


def archive_ojs_seo_project(actor: User, journal_id: str) -> OjsJournal:
    """Move um projeto OJS ativo para a área de arquivados sem apagar dados.

    A reserva comercial permanece ativa: arquivar não libera capacidade nem
    permite que uma assinatura contorne a cota por sucessivas restaurações.
    A transação curta bloqueia o periódico e as auditorias abertas, impedindo
    que um worker grave sobre um projeto que acabou de ser arquivado.
    """
    require_ojs_seo_access(actor)
    _require_clean_local_session()
    try:
        with _exclusive_reservation_transaction():
            journal = _locked_journal_for_actor_in_transaction(actor, journal_id)
            project = journal.analytics_project
            if project is None or project.module_key != OJS_SEO_PROJECT_MODULE:
                raise OjsSeoNotFoundError(
                    "ojs_journal_not_found", "O periódico OJS não foi encontrado."
                )
            if project.status != "active" or project.archived_at is not None:
                raise OjsSeoProjectLifecycleError(
                    "ojs_project_not_active", "Somente periódicos ativos podem ser arquivados."
                )
            _reject_open_ojs_audits(journal, "arquivar")

            # Confirma que o titular ainda pode administrar qualquer reserva
            # existente, mesmo se a assinatura tiver expirado. Arquivar é uma
            # operação de preservação; não concede nova capacidade comercial.
            allocations = list(db.session.scalars(_locked_statement(
                select(WorkspaceSubscriptionOjsJournal).where(
                    WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id,
                )
            )))
            for allocation in allocations:
                _subscription_for_actor(
                    actor,
                    allocation.workspace_subscription_id,
                    require_capacity=False,
                    require_operational_access=False,
                    require_active_subscription=False,
                )

            if not allocations:
                _require_administrative_journal_context(actor, journal)

            project.status = "archived"
            project.archived_at = utcnow()
            db.session.flush()
            record_audit(actor, "ojs_seo_project_archived", "analytics_project", project.id, {
                "status": "active", "owner_user_id": project.owner_user_id,
            }, {
                "status": "archived", "owner_user_id": project.owner_user_id,
            })
            return journal
    except OperationalError as exc:
        raise OjsSeoProjectLifecycleError(
            "ojs_project_archive_busy",
            "Não foi possível arquivar o periódico agora porque há outra operação em andamento. Tente novamente.",
        ) from exc


def restore_ojs_seo_project(actor: User, journal_id: str) -> OjsJournal:
    """Restaura um projeto OJS arquivado sem criar nem liberar reservas.

    A restauração exige que a mesma alocação comercial ainda esteja ativa e
    que a assinatura continue operacionalmente elegível. Isso evita reativar
    um periódico arquivado para contornar expiração, cancelamento ou remoção
    do direito comercial. Como a reserva foi preservada no arquivamento, a
    restauração não consome uma segunda unidade da cota.
    """
    require_ojs_seo_access(actor)
    _require_clean_local_session()
    try:
        with _exclusive_reservation_transaction():
            journal = _locked_journal_for_actor_in_transaction(actor, journal_id)
            project = journal.analytics_project
            if project is None or project.module_key != OJS_SEO_PROJECT_MODULE:
                raise OjsSeoNotFoundError(
                    "ojs_journal_not_found", "O periódico OJS não foi encontrado."
                )
            if project.status != "archived" or project.archived_at is None:
                raise OjsSeoProjectLifecycleError(
                    "ojs_project_not_archived", "Somente periódicos arquivados podem ser restaurados."
                )
            _reject_open_ojs_audits(journal, "restaurar")

            if _journal_uses_administrative_context(journal):
                _require_administrative_journal_context(actor, journal)
            else:
                allocation = db.session.scalar(_locked_statement(
                    select(WorkspaceSubscriptionOjsJournal).where(
                        WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id,
                        WorkspaceSubscriptionOjsJournal.status == "active",
                    )
                ))
                if allocation is None:
                    raise OjsSeoSubscriptionError(
                        "ojs_journal_allocation_inactive",
                        "O periódico não possui uma alocação comercial ativa para ser restaurado.",
                    )
                _subscription_for_actor(
                    actor,
                    allocation.workspace_subscription_id,
                    require_capacity=True,
                )

            project.status = "active"
            project.archived_at = None
            db.session.flush()
            record_audit(actor, "ojs_seo_project_restored", "analytics_project", project.id, {
                "status": "archived", "owner_user_id": project.owner_user_id,
            }, {
                "status": "active", "owner_user_id": project.owner_user_id,
            })
            return journal
    except OperationalError as exc:
        raise OjsSeoProjectLifecycleError(
            "ojs_project_restore_busy",
            "Não foi possível restaurar o periódico agora porque há outra operação em andamento. Tente novamente.",
        ) from exc


def _reject_open_ojs_audits(journal: OjsJournal, action: str) -> None:
    """Impede transições enquanto um worker ainda possa usar o periódico."""
    open_run = db.session.scalar(
        _locked_statement(
            select(OjsSeoAuditRun.id).where(
                OjsSeoAuditRun.journal_id == journal.id,
                OjsSeoAuditRun.status.in_(OPEN_AUDIT_STATUSES),
            )
        ).limit(1)
    )
    open_html_run = db.session.scalar(
        _locked_statement(
            select(OjsHtmlAuditRun.id).where(
                OjsHtmlAuditRun.journal_id == journal.id,
                OjsHtmlAuditRun.status.in_(OPEN_AUDIT_STATUSES),
            )
        ).limit(1)
    )
    if open_run is not None or open_html_run is not None:
        raise OjsSeoProjectLifecycleError(
            "ojs_project_audit_open",
            f"Não é possível {action} o periódico enquanto houver uma auditoria pendente ou em execução.",
        )


def _delete_ojs_project_records(journal: OjsJournal, project: AnalyticsProject) -> None:
    """Remove explicitamente somente os dados dependentes do domínio OJS."""
    db.session.execute(delete(OjsArticleHtmlAnalysis).where(
        OjsArticleHtmlAnalysis.journal_id == journal.id
    ))
    # A operação integrada referencia tanto a coleta OAI quanto a campanha
    # HTML por FKs compostas. Ela precisa sair antes dos seus registros
    # históricos; isso também permite excluir normalmente um periódico cuja
    # etapa HTML tenha sido cancelada de forma segura.
    db.session.execute(delete(OjsSeoAnalysisOperation).where(
        OjsSeoAnalysisOperation.journal_id == journal.id
    ))
    db.session.execute(delete(OjsHtmlAuditRun).where(OjsHtmlAuditRun.journal_id == journal.id))
    db.session.execute(delete(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.journal_id == journal.id
    ))
    db.session.execute(delete(OjsArticleObservation).where(
        OjsArticleObservation.journal_id == journal.id
    ))
    db.session.execute(delete(OjsArticle).where(OjsArticle.journal_id == journal.id))
    db.session.execute(delete(OjsSeoAuditRun).where(OjsSeoAuditRun.journal_id == journal.id))
    db.session.execute(delete(WorkspaceSubscriptionOjsJournal).where(
        WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id
    ))
    db.session.execute(delete(OjsJournal).where(OjsJournal.id == journal.id))
    db.session.execute(delete(AnalyticsProject).where(
        AnalyticsProject.id == project.id,
        AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE,
    ))


def list_ojs_journals(actor: User, *, include_inactive: bool = False) -> list[OjsJournal]:
    """Lista somente periódicos OJS SEO pertencentes à conta, salvo admin."""
    require_ojs_seo_access(actor)
    statement = (
        select(OjsJournal)
        .join(AnalyticsProject, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE)
        .order_by(OjsJournal.created_at.desc(), OjsJournal.id.desc())
    )
    if actor.role != "admin":
        statement = statement.where(AnalyticsProject.owner_user_id == actor.id)
    if not include_inactive:
        statement = statement.where(
            AnalyticsProject.status == "active",
            AnalyticsProject.archived_at.is_(None),
        )
    return list(db.session.scalars(statement))


def list_archived_ojs_journals(actor: User) -> list[OjsJournal]:
    """Lista somente periódicos arquivados no escopo autorizado."""
    require_ojs_seo_access(actor)
    statement = (
        select(OjsJournal)
        .join(AnalyticsProject, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(
            AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE,
            AnalyticsProject.status == "archived",
            AnalyticsProject.archived_at.is_not(None),
        )
        .order_by(AnalyticsProject.archived_at.desc(), OjsJournal.id.desc())
    )
    if actor.role != "admin":
        statement = statement.where(AnalyticsProject.owner_user_id == actor.id)
    return list(db.session.scalars(statement))


def list_ojs_seo_subscriptions_for_actor(actor: User) -> list[WorkspaceSubscription]:
    """Retorna somente assinaturas que podem iniciar um cadastro OJS.

    A lista serve ao formulário, mas não é uma autorização em si: a reserva
    repete todas as verificações numa transação curta depois da descoberta
    externa. Membros comuns ficam fora até existir uma matriz de papéis que
    lhes conceda administração comercial explícita.
    """
    require_ojs_seo_access(actor)
    statement = (
        select(WorkspaceSubscription)
        .join(Workspace, WorkspaceSubscription.workspace_id == Workspace.id)
        .where(
            WorkspaceSubscription.status == "active",
            (WorkspaceSubscription.ended_at.is_(None) | (WorkspaceSubscription.ended_at > utcnow())),
            Workspace.status == "active",
        )
        .order_by(Workspace.created_at.asc(), WorkspaceSubscription.started_at.desc())
    )
    if actor.role != "admin":
        statement = statement.join(
            WorkspaceMembership,
            (WorkspaceMembership.workspace_id == Workspace.id)
            & (WorkspaceMembership.user_id == actor.id)
            & (WorkspaceMembership.status == "active")
            & (WorkspaceMembership.role == "owner"),
        )

    eligible: list[WorkspaceSubscription] = []
    for subscription in db.session.scalars(statement).unique():
        if not subscription.plan.active:
            continue
        if db.session.get(PlanTool, (subscription.plan_id, OJS_SEO_TOOL_ID)) is None:
            continue
        try:
            limit = _require_available_capacity(subscription)
        except OjsSeoQuotaError:
            continue
        if _active_allocation_count(subscription.id) < limit:
            eligible.append(subscription)
    return eligible


def list_ojs_seo_subscription_usage_for_actor(actor: User) -> list[OjsSeoSubscriptionUsage]:
    """Retorna a capacidade contratual OJS das assinaturas atuais do ator.

    Diferentemente da lista usada pelo formulário de cadastro, esta função
    também retorna assinaturas cuja capacidade já está totalmente utilizada.
    Assim, a interface não confunde uma assinatura cheia com a inexistência de
    uma assinatura e não deixa de contabilizar periódicos arquivados.
    """
    require_ojs_seo_access(actor)
    statement = (
        select(WorkspaceSubscription)
        .join(Workspace, WorkspaceSubscription.workspace_id == Workspace.id)
        .where(
            WorkspaceSubscription.status == "active",
            (WorkspaceSubscription.ended_at.is_(None) | (WorkspaceSubscription.ended_at > utcnow())),
            Workspace.status == "active",
        )
        .order_by(Workspace.created_at.asc(), WorkspaceSubscription.started_at.desc())
    )
    if actor.role != "admin":
        statement = statement.join(
            WorkspaceMembership,
            (WorkspaceMembership.workspace_id == Workspace.id)
            & (WorkspaceMembership.user_id == actor.id)
            & (WorkspaceMembership.status == "active")
            & (WorkspaceMembership.role == "owner"),
        )

    usages: list[OjsSeoSubscriptionUsage] = []
    for subscription in db.session.scalars(statement).unique():
        if not subscription.plan.active:
            continue
        if db.session.get(PlanTool, (subscription.plan_id, OJS_SEO_TOOL_ID)) is None:
            continue
        limits = effective_subscription_limits(subscription, at=utcnow())
        resource = limits["resources"].get("ojs_journals", {})
        raw_limit = resource.get("effective_limit", 0)
        contracted_limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 0
        contracted_limit = max(0, contracted_limit)
        used_slots = _active_allocation_count(subscription.id)
        usages.append(OjsSeoSubscriptionUsage(
            subscription=subscription,
            used_slots=used_slots,
            contracted_limit=contracted_limit,
            available_slots=max(0, contracted_limit - used_slots),
        ))
    return usages


def get_ojs_journal(actor: User, journal_id: str, *, include_inactive: bool = False) -> OjsJournal:
    """Obtém um periódico somente dentro do escopo do proprietário/admin."""
    require_ojs_seo_access(actor)
    if not isinstance(journal_id, str) or not journal_id:
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    statement = (
        select(OjsJournal)
        .join(AnalyticsProject, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(
            OjsJournal.id == journal_id,
            AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE,
        )
    )
    if actor.role != "admin":
        statement = statement.where(AnalyticsProject.owner_user_id == actor.id)
    if not include_inactive:
        statement = statement.where(AnalyticsProject.status == "active")
    journal = db.session.scalar(statement)
    if journal is None:
        # A mesma resposta para IDs desconhecidos e recursos de terceiros evita
        # transformar este serviço em um oráculo de existência.
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    return journal


def request_ojs_seo_audit(actor: User, journal_id: str) -> OjsSeoAuditRun:
    """Cria uma única auditoria pendente para o periódico acessível.

    Esta função não faz HTTP nem executa o worker. A camada chamadora controla
    a transação externa e deve confirmar a auditoria antes de iniciar qualquer
    processo independente.
    """
    journal = get_ojs_journal(actor, journal_id)
    _require_active_journal_allocation(actor, journal)
    if not journal.oai_endpoint_url or journal.discovery_status != "identified":
        raise OjsSeoServiceError(
            "oai_endpoint_unavailable", "O periódico ainda não possui um endpoint OAI-PMH confirmado."
        )
    existing = db.session.scalar(
        select(OjsSeoAuditRun.id).where(
            OjsSeoAuditRun.journal_id == journal.id,
            OjsSeoAuditRun.status.in_(OPEN_AUDIT_STATUSES),
        )
    )
    if existing is not None:
        raise OjsSeoServiceError(
            "ojs_audit_already_open", "Já existe uma auditoria pendente ou em processamento para este periódico."
        )
    html_open = db.session.scalar(
        select(OjsHtmlAuditRun.id).where(
            OjsHtmlAuditRun.journal_id == journal.id,
            OjsHtmlAuditRun.status.in_(OPEN_AUDIT_STATUSES),
        ).limit(1)
    )
    if html_open is not None:
        raise OjsSeoServiceError(
            "ojs_html_audit_open",
            "Já existe uma análise técnica HTML pendente ou em processamento para este periódico.",
        )
    run = OjsSeoAuditRun(
        journal_id=journal.id,
        status="pending",
        parameters_json={"metadata_prefix": "oai_dc"},
    )
    db.session.add(run)
    db.session.flush()
    record_audit(actor, "ojs_seo_audit_requested", "ojs_seo_audit_run", run.id, {}, {
        "journal_id": journal.id,
        "status": run.status,
        "requested_at": utcnow().isoformat(),
    })
    return run


def cancel_pending_ojs_seo_audit(actor: User, audit_run_id: str) -> OjsSeoAuditRun:
    """Cancela uma pendência sem disputar ou sobrescrever um claim do worker.

    A transação é curta e serializada: PostgreSQL bloqueia a linha da execução
    e SQLite usa ``BEGIN IMMEDIATE``. O ``UPDATE`` também repete o estado
    ``pending`` e a ausência de token/lease, portanto um worker que tenha
    reivindicado a auditoria entre a leitura e a escrita vence a corrida e o
    cancelamento é recusado sem alterar a execução em andamento.
    """
    require_ojs_seo_access(actor)
    if not isinstance(audit_run_id, str) or not audit_run_id:
        raise OjsSeoNotFoundError("ojs_audit_not_found", "A auditoria OJS não foi encontrada.")
    _require_clean_local_session()
    try:
        with _exclusive_reservation_transaction():
            statement = (
                select(OjsSeoAuditRun)
                .join(OjsJournal, OjsSeoAuditRun.journal_id == OjsJournal.id)
                .join(AnalyticsProject, OjsJournal.analytics_project_id == AnalyticsProject.id)
                .where(
                    OjsSeoAuditRun.id == audit_run_id,
                    AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE,
                )
            )
            if actor.role != "admin":
                statement = statement.where(AnalyticsProject.owner_user_id == actor.id)
            run = db.session.scalar(_locked_statement(statement))
            if run is None:
                # Não diferencia uma auditoria inexistente de outra conta.
                raise OjsSeoNotFoundError("ojs_audit_not_found", "A auditoria OJS não foi encontrada.")
            if run.status != "pending":
                raise OjsSeoAuditCancellationError(
                    "ojs_audit_not_pending",
                    "Somente auditorias pendentes podem ser canceladas.",
                )

            completed_at = utcnow()
            result = db.session.execute(
                update(OjsSeoAuditRun)
                .where(
                    OjsSeoAuditRun.id == run.id,
                    OjsSeoAuditRun.journal_id == run.journal_id,
                    OjsSeoAuditRun.status == "pending",
                    OjsSeoAuditRun.claim_token.is_(None),
                    OjsSeoAuditRun.claimed_at.is_(None),
                    OjsSeoAuditRun.lease_expires_at.is_(None),
                )
                .values(
                    status="canceled",
                    completed_at=completed_at,
                    completion_reason="canceled_by_user",
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                )
            )
            if result.rowcount != 1:
                raise OjsSeoAuditCancellationError(
                    "ojs_audit_claimed_before_cancel",
                    "A auditoria foi assumida pelo processamento e não pode mais ser cancelada.",
                )
            db.session.refresh(run)
            record_audit(actor, "ojs_seo_audit_canceled", "ojs_seo_audit_run", run.id, {
                "status": "pending",
                "journal_id": run.journal_id,
            }, {
                "status": "canceled",
                "completion_reason": "canceled_by_user",
                "completed_at": completed_at.isoformat(),
            })
            return run
    except OperationalError as exc:
        raise OjsSeoAuditCancellationError(
            "ojs_audit_cancel_busy",
            "Não foi possível cancelar a auditoria agora porque há outra operação em andamento. Tente novamente.",
        ) from exc


def cancel_pending_ojs_html_audit(
    actor: User,
    journal_id: str,
    audit_run_id: str,
) -> OjsHtmlAuditRun:
    """Cancela somente a etapa HTML ainda pendente de um periódico autorizado.

    A campanha pode já ter começado em lotes anteriores, mas o cancelamento só
    é permitido quando o lote atualmente aberto ainda não tem claim. O lock do
    periódico é o mesmo utilizado pelo worker HTML: a operação administrativa
    e o claim concorrem de forma atômica, sem permitir que um resultado HTTP
    seja descartado depois de iniciado.
    """
    require_ojs_seo_access(actor)
    if not isinstance(journal_id, str) or not journal_id:
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    if not isinstance(audit_run_id, str) or not audit_run_id:
        raise OjsSeoNotFoundError("ojs_html_audit_not_found", "A análise técnica HTML não foi encontrada.")
    _require_clean_local_session()
    try:
        with _exclusive_reservation_transaction():
            # O worker HTML também bloqueia este periódico antes de fazer seu
            # compare-and-set. Em SQLite, BEGIN IMMEDIATE oferece a mesma
            # serialização para todos os registros da transação curta.
            journal = _locked_journal_for_actor_in_transaction(actor, journal_id)
            run = db.session.scalar(_locked_statement(select(OjsHtmlAuditRun).where(
                OjsHtmlAuditRun.id == audit_run_id,
                OjsHtmlAuditRun.journal_id == journal.id,
            )))
            if run is None:
                raise OjsSeoNotFoundError(
                    "ojs_html_audit_not_found", "A análise técnica HTML não foi encontrada."
                )
            # Repetir uma confirmação já concluída não reabre a fila nem
            # altera o histórico. A primeira gravação já deixou campanha e
            # operação integradas em estado terminal.
            if run.status == "canceled":
                return run
            if run.status != "pending":
                raise OjsSeoAuditCancellationError(
                    "ojs_html_audit_not_pending",
                    "Somente análises das páginas pendentes podem ser canceladas.",
                )

            campaign = None
            operation = None
            if run.campaign_id:
                campaign = db.session.scalar(_locked_statement(select(OjsHtmlAuditCampaign).where(
                    OjsHtmlAuditCampaign.id == run.campaign_id,
                    OjsHtmlAuditCampaign.journal_id == journal.id,
                )))
                if campaign is None:
                    raise OjsSeoAuditCancellationError(
                        "ojs_html_campaign_not_found",
                        "A campanha da análise das páginas não está disponível para cancelamento.",
                    )
                if campaign.status not in {"pending", "running"}:
                    raise OjsSeoAuditCancellationError(
                        "ojs_html_campaign_not_open",
                        "A campanha da análise das páginas não está em condição segura para cancelamento.",
                    )
                # A restrição de fila já impede dois lotes abertos, mas a
                # checagem explícita preserva a regra caso um histórico legado
                # não tenha sido criado sob a restrição atual.
                running_id = db.session.scalar(_locked_statement(select(OjsHtmlAuditRun.id).where(
                    OjsHtmlAuditRun.journal_id == journal.id,
                    OjsHtmlAuditRun.status == "running",
                ).limit(1)))
                if running_id is not None:
                    raise OjsSeoAuditCancellationError(
                        "ojs_html_audit_claimed_before_cancel",
                        "A análise das páginas já está em execução e não pode mais ser cancelada.",
                    )
                operation = db.session.scalar(_locked_statement(select(OjsSeoAnalysisOperation).where(
                    OjsSeoAnalysisOperation.html_campaign_id == campaign.id,
                    OjsSeoAnalysisOperation.journal_id == journal.id,
                )))
                if operation is not None and operation.status not in {"html_pending", "html_running"}:
                    raise OjsSeoAuditCancellationError(
                        "ojs_integrated_html_not_open",
                        "A operação integrada não está em condição segura para cancelar a etapa HTML.",
                    )

            completed_at = utcnow()
            result = db.session.execute(
                update(OjsHtmlAuditRun)
                .where(
                    OjsHtmlAuditRun.id == run.id,
                    OjsHtmlAuditRun.journal_id == journal.id,
                    OjsHtmlAuditRun.status == "pending",
                    OjsHtmlAuditRun.claim_token.is_(None),
                    OjsHtmlAuditRun.claimed_at.is_(None),
                    OjsHtmlAuditRun.lease_expires_at.is_(None),
                )
                .values(
                    status="canceled",
                    completed_at=completed_at,
                    completion_reason="canceled_by_user",
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                )
            )
            if result.rowcount != 1:
                raise OjsSeoAuditCancellationError(
                    "ojs_html_audit_claimed_before_cancel",
                    "A análise das páginas foi assumida pelo processamento e não pode mais ser cancelada.",
                )
            db.session.refresh(run)

            if campaign is not None:
                previous_campaign_status = campaign.status
                campaign.status = "canceled"
                campaign.completion_reason = "canceled_by_user"
                campaign.completed_at = completed_at
                campaign.updated_at = completed_at
                record_audit(actor, "ojs_seo_html_campaign_canceled", "ojs_html_audit_campaign", campaign.id, {
                    "status": previous_campaign_status,
                    "journal_id": journal.id,
                }, {
                    "status": "canceled",
                    "completion_reason": "canceled_by_user",
                    "completed_at": completed_at.isoformat(),
                })

            if operation is not None:
                previous_operation_status = operation.status
                operation.status = "canceled"
                operation.completion_reason = "canceled_by_user"
                operation.completed_at = completed_at
                operation.updated_at = completed_at
                record_audit(actor, "ojs_seo_integrated_html_canceled", "ojs_seo_analysis_operation", operation.id, {
                    "status": previous_operation_status,
                    "journal_id": journal.id,
                    "html_campaign_id": campaign.id if campaign is not None else None,
                }, {
                    "status": "canceled",
                    "completion_reason": "canceled_by_user",
                    "completed_at": completed_at.isoformat(),
                })

            record_audit(actor, "ojs_seo_html_audit_canceled", "ojs_html_audit_run", run.id, {
                "status": "pending",
                "journal_id": journal.id,
                "campaign_id": run.campaign_id,
            }, {
                "status": "canceled",
                "completion_reason": "canceled_by_user",
                "completed_at": completed_at.isoformat(),
            })
            return run
    except OperationalError as exc:
        raise OjsSeoAuditCancellationError(
            "ojs_html_audit_cancel_busy",
            "Não foi possível cancelar a análise das páginas agora porque há outra operação em andamento. Tente novamente.",
        ) from exc


def can_request_ojs_seo_audit(actor: User, journal_id: str) -> bool:
    """Indica se a interface pode oferecer uma nova auditoria com segurança.

    É uma conveniência de apresentação, não uma autorização substituta: a rota
    sempre chama ``request_ojs_seo_audit`` de novo, porque assinatura, quota e
    alocação podem mudar entre a renderização e o POST.
    """
    try:
        journal = get_ojs_journal(actor, journal_id)
        _require_active_journal_allocation(actor, journal)
    except OjsSeoServiceError:
        return False
    if not journal.oai_endpoint_url or journal.discovery_status != "identified":
        return False
    return db.session.scalar(
        select(OjsSeoAuditRun.id).where(
            OjsSeoAuditRun.journal_id == journal.id,
            OjsSeoAuditRun.status.in_(OPEN_AUDIT_STATUSES),
        )
    ) is None


def list_ojs_seo_audits(
    actor: User, journal_id: str, *, include_inactive: bool = False,
) -> list[OjsSeoAuditRun]:
    """Retorna somente o histórico de auditorias de periódico autorizado."""
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return list(db.session.scalars(
        select(OjsSeoAuditRun)
        .where(OjsSeoAuditRun.journal_id == journal.id)
        .order_by(OjsSeoAuditRun.created_at.desc(), OjsSeoAuditRun.id.desc())
    ))


def get_ojs_seo_audit_result(
    actor: User, audit_run_id: str, *, include_inactive: bool = False,
) -> OjsSeoAuditRun:
    """Obtém uma execução e confirma seu periódico antes de devolvê-la."""
    require_ojs_seo_access(actor)
    if not isinstance(audit_run_id, str) or not audit_run_id:
        raise OjsSeoNotFoundError("ojs_audit_not_found", "A auditoria OJS não foi encontrada.")
    run = db.session.get(OjsSeoAuditRun, audit_run_id)
    if run is None:
        raise OjsSeoNotFoundError("ojs_audit_not_found", "A auditoria OJS não foi encontrada.")
    # Reaproveita a checagem de projeto/módulo/proprietário, sem expor a relação
    # entre uma auditoria inexistente e uma auditoria de outra pessoa.
    get_ojs_journal(actor, run.journal_id, include_inactive=include_inactive)
    return run


def list_ojs_articles(
    actor: User, journal_id: str, *, include_inactive: bool = False,
) -> list[OjsArticle]:
    """Lista registros atuais somente depois de validar o periódico-pai."""
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return list(db.session.scalars(
        select(OjsArticle)
        .where(OjsArticle.journal_id == journal.id)
        .order_by(OjsArticle.last_observed_at.desc(), OjsArticle.id.desc())
    ))


def get_ojs_article(
    actor: User, journal_id: str, article_id: str, *, include_inactive: bool = False,
) -> OjsArticle:
    """Obtém um artigo sem transformar IDs de terceiros em oráculo."""
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    if not isinstance(article_id, str) or not article_id:
        raise OjsSeoNotFoundError("ojs_article_not_found", "O registro bibliográfico não foi encontrado.")
    article = db.session.scalar(select(OjsArticle).where(
        OjsArticle.id == article_id,
        OjsArticle.journal_id == journal.id,
    ))
    if article is None:
        raise OjsSeoNotFoundError("ojs_article_not_found", "O registro bibliográfico não foi encontrado.")
    return article


def list_ojs_article_observations(
    actor: User, journal_id: str, article_id: str, *, include_inactive: bool = False,
) -> list[OjsArticleObservation]:
    """Devolve apenas evidências históricas pertencentes ao registro autorizado."""
    article = get_ojs_article(
        actor, journal_id, article_id, include_inactive=include_inactive,
    )
    return list(db.session.scalars(
        select(OjsArticleObservation)
        .where(OjsArticleObservation.article_id == article.id)
        .order_by(OjsArticleObservation.observed_at.desc(), OjsArticleObservation.id.desc())
    ))


def _validate_declared_public_url(value: str) -> str:
    """Valida a URL informada sem buscá-la nem aceitar credenciais embutidas."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2_048:
        raise OjsSeoDiscoveryError("invalid_public_url", "Informe a URL pública HTTPS do periódico.")
    if any(ord(char) < 32 for char in value):
        raise OjsSeoDiscoveryError("invalid_public_url", "A URL pública contém caracteres não permitidos.")
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise OjsSeoDiscoveryError("invalid_public_url", "A URL pública do periódico é inválida.") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise OjsSeoDiscoveryError("invalid_public_url", "Informe uma URL pública HTTPS sem credenciais.")
    return value.strip()


def suggested_oai_endpoint(public_url: str) -> str:
    """Deriva somente o endpoint convencional OJS ``/oai`` de uma URL pública.

    Não há tentativa de varrer caminhos, seguir redirecionamentos ou acessar
    HTML. A URL resultante ainda passa por ``validate_oai_endpoint`` antes de
    qualquer conexão; se a instalação usar um endpoint não convencional, a
    descoberta falha de forma segura e poderá receber suporte explícito em uma
    evolução futura do formulário.
    """
    normalized = _validate_declared_public_url(public_url)
    parsed = urlsplit(normalized)
    if parsed.query or parsed.fragment:
        raise OjsSeoDiscoveryError(
            "invalid_public_url", "Informe a URL base do periódico, sem parâmetros ou fragmentos."
        )
    path = (parsed.path or "/").rstrip("/")
    endpoint_path = path if path.endswith("/oai") else f"{path}/oai"
    return f"https://{parsed.netloc}{endpoint_path}"


def canonical_ojs_journal_identity(canonical_endpoint_url: str) -> str:
    """Retorna a identidade da revista derivada do endpoint OAI validado.

    O cliente normaliza esquema/host/porta e rejeita redirecionamentos antes de
    produzir ``canonical_url``. A parte de caminho é deliberadamente mantida:
    instalações OJS hospedam várias revistas e seus endpoints normalmente se
    diferenciam nela. URLs que diferem apenas por uma barra final não são
    assumidas equivalentes nesta fase, pois o cliente não segue redirect; uma
    reconciliação explícita poderá ser criada quando houver evidência de que o
    fornecedor trata as duas formas como o mesmo repositório.
    """
    if not isinstance(canonical_endpoint_url, str) or not canonical_endpoint_url:
        raise OjsSeoServiceError("invalid_ojs_identity", "O endpoint OAI-PMH confirmado é inválido.")
    return hashlib.sha256(
        f"ojs-oai-endpoint-v1\x00{canonical_endpoint_url}".encode("utf-8")
    ).hexdigest()


def _validate_project_name(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 200:
        raise OjsSeoServiceError("invalid_project_name", "Informe um nome de periódico com até 200 caracteres.")
    return normalized


def _validate_project_description(value: str) -> str:
    normalized = (value or "").strip()
    if len(normalized) > 20_000:
        raise OjsSeoServiceError("invalid_project_description", "A descrição do periódico é muito longa.")
    return normalized


def _require_clean_local_session() -> None:
    if db.session.new or db.session.dirty or db.session.deleted:
        raise OjsSeoServiceError(
            "reservation_requires_clean_session",
            "A reserva comercial deve começar sem alterações pendentes no banco.",
        )
    db.session.rollback()


@contextmanager
def _exclusive_reservation_transaction():
    """Serializa a contagem e a reserva entre processos.

    PostgreSQL bloqueia a linha da assinatura com ``FOR UPDATE`` dentro da
    transação. SQLite não oferece esse lock de linha, então ``BEGIN IMMEDIATE``
    adquire antecipadamente o lock de escrita: outro processo não consegue ler
    uma contagem obsoleta e inserir uma segunda reserva antes do commit. A
    transação contém apenas consultas e gravações locais; a descoberta HTTP
    termina antes desta função ser chamada.
    """
    session = db.session
    if session.new or session.dirty or session.deleted:
        raise OjsSeoServiceError(
            "reservation_requires_clean_session",
            "A reserva comercial deve começar sem alterações pendentes no banco.",
        )
    dialect = session.get_bind().dialect.name
    try:
        if dialect == "sqlite":
            # Deve ser a primeira instrução da transação. O chamador limpa a
            # sessão depois da descoberta e antes de iniciar a reserva.
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            session.begin()
        yield
        session.commit()
    except Exception:
        session.rollback()
        raise


def _registration_context_for_actor(
    actor: User, workspace_subscription_id: str | None,
) -> OjsSeoJournalRegistrationContext:
    """Resolve a única origem legítima de autorização para um novo periódico.

    Assinantes continuam obrigados a informar uma assinatura elegível. O único
    caminho sem assinatura é o contexto administrativo explícito; ele não cria
    ou reutiliza contratos comerciais e é revalidado depois da descoberta.
    """
    subscription_id = workspace_subscription_id.strip() if isinstance(workspace_subscription_id, str) else ""
    if subscription_id:
        return OjsSeoJournalRegistrationContext(
            mode="subscription",
            subscription=_subscription_for_actor(actor, subscription_id, require_capacity=True),
        )
    if has_administrative_ojs_context(actor):
        return OjsSeoJournalRegistrationContext(mode="administrator")
    require_ojs_seo_access(actor)
    raise OjsSeoSubscriptionError(
        "ojs_subscription_not_found",
        "Nenhuma assinatura elegível foi encontrada para cadastrar um periódico OJS.",
    )


def _subscription_for_actor(
    actor: User,
    subscription_id: str,
    *,
    require_capacity: bool,
    require_operational_access: bool = True,
    require_active_subscription: bool = True,
) -> WorkspaceSubscription:
    """Valida subscription, workspace, titularidade e ferramenta na transação."""
    if not isinstance(actor, User) or not actor.id or not actor.is_active:
        raise OjsSeoAuthorizationError(
            "ojs_seo_subscription_access_denied", "A conta não está autorizada para esta assinatura."
        )
    if not isinstance(subscription_id, str) or not subscription_id:
        raise OjsSeoSubscriptionError("ojs_subscription_not_found", "A assinatura OJS não foi encontrada.")
    statement = select(WorkspaceSubscription).where(WorkspaceSubscription.id == subscription_id)
    if db.session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    subscription = db.session.scalar(statement)
    if subscription is None:
        raise OjsSeoSubscriptionError("ojs_subscription_not_found", "A assinatura OJS não foi encontrada.")
    workspace = db.session.get(Workspace, subscription.workspace_id)
    if workspace is None or workspace.status != "active":
        raise OjsSeoSubscriptionError(
            "ojs_workspace_inactive", "A área de trabalho da assinatura não está ativa."
        )
    if actor.role != "admin":
        membership = db.session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.user_id == actor.id,
                WorkspaceMembership.status == "active",
            ).order_by(WorkspaceMembership.joined_at.desc(), WorkspaceMembership.id.desc())
        )
        # Os papéis detalhados de equipe ainda não existem. Nesta fase somente
        # o titular pode consumir ou liberar capacidade compartilhada.
        if membership is None or membership.role != "owner":
            raise OjsSeoAuthorizationError(
                "ojs_seo_subscription_access_denied",
                "A conta não está autorizada para administrar a assinatura OJS."
            )
    now = utcnow()
    if require_active_subscription:
        if subscription.status != "active" or (
            subscription.ended_at is not None and _as_utc(subscription.ended_at) <= now
        ):
            raise OjsSeoSubscriptionError(
                "ojs_subscription_inactive", "A assinatura não está ativa para periódicos OJS."
            )
        if not subscription.plan.active:
            raise OjsSeoSubscriptionError(
                "ojs_plan_inactive", "O plano da assinatura não está ativo para periódicos OJS."
            )
    if require_operational_access:
        require_ojs_seo_access(actor)
        if db.session.get(PlanTool, (subscription.plan_id, OJS_SEO_TOOL_ID)) is None:
            raise OjsSeoSubscriptionError(
                "ojs_plan_tool_missing", "O plano da assinatura não autoriza OJS SEO."
            )
    if require_capacity:
        _require_available_capacity(subscription, at=now)
    return subscription


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _require_available_capacity(subscription: WorkspaceSubscription, *, at: datetime | None = None) -> int:
    limits = effective_subscription_limits(subscription, at=at or utcnow())
    resource = limits["resources"].get("ojs_journals", {})
    limit = resource.get("effective_limit", 0)
    if not isinstance(limit, int) or limit <= 0:
        raise OjsSeoQuotaError(
            "ojs_journal_quota_unavailable", "A assinatura não possui capacidade ativa para periódicos OJS."
        )
    return limit


def _active_allocation_count(subscription_id: str) -> int:
    return int(db.session.scalar(
        select(func.count()).select_from(WorkspaceSubscriptionOjsJournal).where(
            WorkspaceSubscriptionOjsJournal.workspace_subscription_id == subscription_id,
            WorkspaceSubscriptionOjsJournal.status == "active",
        )
    ) or 0)


def _reject_active_identity_duplicate(subscription_id: str, identity_hash: str) -> None:
    existing = db.session.scalar(
        select(WorkspaceSubscriptionOjsJournal.id).where(
            WorkspaceSubscriptionOjsJournal.workspace_subscription_id == subscription_id,
            WorkspaceSubscriptionOjsJournal.canonical_identity_hash == identity_hash,
            WorkspaceSubscriptionOjsJournal.status == "active",
        ).limit(1)
    )
    if existing is not None:
        raise OjsSeoQuotaError(
            "ojs_journal_already_allocated", "Este periódico já possui uma reserva ativa nesta assinatura."
        )


def _reserve_journal_in_transaction(
    subscription: WorkspaceSubscription,
    journal: OjsJournal,
    identity_hash: str,
    endpoint_url: str,
) -> WorkspaceSubscriptionOjsJournal:
    if journal.id is None:
        raise OjsSeoServiceError("ojs_journal_not_persisted", "O periódico não pôde ser preparado para reserva.")
    _reject_active_identity_duplicate(subscription.id, identity_hash)
    limit = _require_available_capacity(subscription)
    if _active_allocation_count(subscription.id) >= limit:
        raise OjsSeoQuotaError(
            "ojs_journal_quota_exceeded",
            "O limite de periódicos da sua assinatura foi atingido. Periódicos arquivados também ocupam vagas.",
        )
    allocation = WorkspaceSubscriptionOjsJournal(
        workspace_subscription_id=subscription.id,
        ojs_journal_id=journal.id,
        canonical_identity_hash=identity_hash,
        canonical_endpoint_url=endpoint_url,
        status="active",
    )
    db.session.add(allocation)
    db.session.flush()
    return allocation


def _journal_for_actor_in_transaction(actor: User, journal_id: str) -> OjsJournal:
    if not isinstance(journal_id, str) or not journal_id:
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    statement = (
        select(OjsJournal)
        .join(AnalyticsProject, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(OjsJournal.id == journal_id, AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE)
    )
    if actor.role != "admin":
        statement = statement.where(AnalyticsProject.owner_user_id == actor.id)
    journal = db.session.scalar(statement)
    if journal is None:
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    return journal


def _locked_journal_for_actor_in_transaction(actor: User, journal_id: str) -> OjsJournal:
    """Obtém e bloqueia o periódico durante uma mutação destrutiva curta."""
    if not isinstance(journal_id, str) or not journal_id:
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    statement = (
        select(OjsJournal)
        .join(AnalyticsProject, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(OjsJournal.id == journal_id, AnalyticsProject.module_key == OJS_SEO_PROJECT_MODULE)
    )
    if actor.role != "admin":
        statement = statement.where(AnalyticsProject.owner_user_id == actor.id)
    journal = db.session.scalar(_locked_statement(statement))
    if journal is None:
        raise OjsSeoNotFoundError("ojs_journal_not_found", "O periódico OJS não foi encontrado.")
    return journal


def _locked_statement(statement):
    """Acrescenta ``FOR UPDATE`` onde o banco suporta lock de linha."""
    if db.session.get_bind().dialect.name == "postgresql":
        return statement.with_for_update()
    return statement


def _journal_uses_administrative_context(journal: OjsJournal) -> bool:
    """Reconhece periódico administrativo sem inferir uma assinatura ausente.

    A única combinação aceita é projeto OJS pertencente a administrador ativo
    e sem qualquer histórico de reserva. Um periódico de assinante nunca entra
    nesse caminho, mesmo se a reserva estiver inativa ou expirada.
    """
    project = journal.analytics_project
    owner = project.owner if project is not None else None
    if (
        project is None
        or project.module_key != OJS_SEO_PROJECT_MODULE
        or owner is None
        or not owner.is_active
        or owner.role != "admin"
    ):
        return False
    return db.session.scalar(
        select(WorkspaceSubscriptionOjsJournal.id)
        .where(WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id)
        .limit(1)
    ) is None


def _require_administrative_journal_context(actor: User, journal: OjsJournal) -> None:
    if not _journal_uses_administrative_context(journal):
        raise OjsSeoSubscriptionError(
            "ojs_journal_allocation_inactive",
            "O periódico não possui uma alocação comercial ativa.",
        )
    _require_administrative_ojs_context(actor)


def _require_active_journal_allocation(
    actor: User, journal: OjsJournal,
) -> WorkspaceSubscriptionOjsJournal | None:
    """Confirma capacidade comercial ou o contexto administrativo persistível."""
    if _journal_uses_administrative_context(journal):
        _require_administrative_journal_context(actor, journal)
        return None
    allocation = db.session.scalar(
        select(WorkspaceSubscriptionOjsJournal).where(
            WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id,
            WorkspaceSubscriptionOjsJournal.status == "active",
        )
    )
    if allocation is None:
        raise OjsSeoSubscriptionError(
            "ojs_journal_allocation_inactive", "O periódico não possui uma alocação comercial ativa."
        )
    # A assinatura precisa continuar ativa e ter capacidade positiva. Uma
    # redução abaixo do número já reservado não apaga dados nem bloqueia as
    # auditorias existentes; redução a zero, expiração ou cancelamento bloqueia
    # novas auditorias até haver uma regularização explícita.
    _subscription_for_actor(actor, allocation.workspace_subscription_id, require_capacity=True)
    return allocation


def journal_has_active_ojs_execution_authorization(journal: OjsJournal) -> bool:
    """Confere autorização atual do periódico para os workers internos.

    O worker não recebe uma identidade de pessoa e não pode inferir que um
    claim antigo continua válido. Periódicos comerciais repetem membership de
    titular, assinatura, plano, ferramenta, ``PlanTool`` e cota positiva.
    Periódicos administrativos exigem proprietário administrador ativo e
    ferramenta operacional, sem criar direito comercial artificial. A função
    não grava nem inicia rede; o executor decide como finalizar pendências.
    """
    project = journal.analytics_project
    if (
        project is None
        or project.module_key != OJS_SEO_PROJECT_MODULE
        or project.status != "active"
        or project.archived_at is not None
        or project.owner is None
    ):
        return False
    if _journal_uses_administrative_context(journal):
        return has_administrative_ojs_context(project.owner)
    allocation = db.session.scalar(
        select(WorkspaceSubscriptionOjsJournal).where(
            WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id,
            WorkspaceSubscriptionOjsJournal.status == "active",
        )
    )
    if allocation is None:
        return False
    try:
        _subscription_for_actor(project.owner, allocation.workspace_subscription_id, require_capacity=True)
    except OjsSeoServiceError:
        return False
    return True


def _end_read_transaction_before_network() -> None:
    """Fecha a leitura de autorização antes da descoberta externa.

    O serviço não pode desfazer alterações que uma futura rota ainda não tenha
    confirmado. Por isso, em vez de fazer ``rollback`` silencioso sobre uma
    sessão suja, ele exige uma sessão limpa para a etapa de rede. O cadastro
    posterior deverá começar uma transação nova, curta e totalmente local.
    """
    if db.session.new or db.session.dirty or db.session.deleted:
        raise OjsSeoServiceError(
            "discovery_requires_clean_session",
            "A descoberta OAI-PMH deve ser iniciada antes de alterações pendentes no banco.",
        )
    db.session.rollback()


def _discovery_from_result(
    public_url: str, endpoint: str, result: OaiPmhAuditResult
) -> OjsJournalDiscovery:
    if not isinstance(result, OaiPmhAuditResult):
        raise OjsSeoDiscoveryError(
            "invalid_discovery_result", "A descoberta OAI-PMH não retornou um resultado reconhecido."
        )
    identity = result.identity
    if result.status not in {"completed", "partial"} or identity is None or result.collection is None:
        issue = result.errors[0] if result.errors else None
        raise OjsSeoDiscoveryError(
            issue.code if issue else "oai_discovery_failed",
            issue.message if issue else "Não foi possível confirmar o endpoint OAI-PMH do periódico.",
        )
    if not identity.repository_name or not identity.base_url or identity.protocol_version != "2.0":
        raise OjsSeoDiscoveryError(
            "invalid_oai_identity", "O endpoint não apresentou uma identificação OAI-PMH 2.0 válida."
        )
    if "oai_dc" not in {item.metadata_prefix for item in result.metadata_formats}:
        raise OjsSeoDiscoveryError(
            "oai_dc_not_advertised", "O endpoint não anunciou metadados Dublin Core oai_dc."
        )
    return OjsJournalDiscovery(
        public_url=public_url,
        endpoint_url=endpoint,
        repository_name=identity.repository_name,
        oai_base_url=identity.base_url,
        protocol_version=identity.protocol_version,
        collection_status=result.collection.status,
    )
