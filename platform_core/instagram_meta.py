"""Providers Meta para conexão, diagnóstico e leitura canônica do Instagram.

Esta camada conhece os contratos específicos de Instagram Login e Facebook
Login. A persistência de mídia, observações e snapshots fica no motor canônico
de sincronização, para que os modelos analíticos não dependam do provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from threading import RLock
from time import monotonic
import secrets
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:  # A dependência é explícita, mas manter o import protegido evita indisponibilidade total.
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - coberto pela configuração indisponível no deploy.
    Fernet = None
    InvalidToken = ValueError

from sqlalchemy import select

from .extensions import db
from .instagram_import import InstagramImportError, normalized_instagram_username
from .models import AnalyticsProject, InstagramAccount, InstagramApiConnection, utcnow


FACEBOOK_LOGIN_SCOPES = (
    "instagram_basic",
    "instagram_manage_insights",
    "instagram_manage_comments",
    "pages_read_engagement",
    "pages_show_list",
)
INSTAGRAM_LOGIN_INSIGHTS_SCOPE = "instagram_business_manage_insights"
INSTAGRAM_LOGIN_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_comments",
    INSTAGRAM_LOGIN_INSIGHTS_SCOPE,
)
META_CONNECTION_PROVIDERS = frozenset({"instagram_login", "facebook_login"})
CAPABILITY_MEDIA_LIMIT = 10
CAPABILITY_COMMENT_MEDIA_LIMIT = 3
CAPABILITY_COMMENTS_PER_MEDIA_LIMIT = 25
CAPABILITY_METRICS = ("reach", "saved", "shares", "total_interactions", "follows", "views")
CAPABILITY_METRIC_SAMPLES_PER_TYPE_LIMIT = 2
PAGE_DISCOVERY_LIMIT = 100
PAGE_DISCOVERY_MAX_PAGES = 20
PAGE_DISCOVERY_FALLBACK_LIMIT = 25
SYNC_MEDIA_PAGE_LIMIT = 100
SYNC_MEDIA_MAX_PAGES = 200
SYNC_MEDIA_FIELDS = (
    "id,caption,comments_count,like_count,media_type,media_product_type,permalink,timestamp"
)


class InstagramMetaError(ValueError):
    """Erro seguro para apresentação, sem token, código ou resposta bruta."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        http_status: int | None = None,
        meta_code: int | str | None = None,
        meta_subcode: int | str | None = None,
        meta_type: str | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.http_status = http_status
        self.meta_code = meta_code
        self.meta_subcode = meta_subcode
        self.meta_type = meta_type


class InstagramMetaConfigurationError(InstagramMetaError):
    def __init__(self, message: str = "A integração com a API do Instagram não está configurada."):
        super().__init__("configuration", message)


def safe_meta_error_message(error: InstagramMetaError) -> str:
    """Converte categorias internas em mensagens estáveis, sem ecoar dados externos."""
    messages = {
        "configuration": "A integração com a API do Instagram não está configurada neste ambiente.",
        "reconnect_required": "Reconecte sua conta do Instagram antes de continuar.",
        "auth_expired": "A autorização do Facebook expirou. Reconecte sua conta do Instagram.",
        "no_managed_pages": "Nenhuma Página do Facebook gerenciada foi encontrada para esta conta.",
        "no_linked_instagram_account": "Não encontramos uma Página do Facebook vinculada a uma conta profissional do Instagram que possa ser usada neste projeto.",
        "multiple_instagram_accounts": "Há mais de uma conta profissional elegível. Escolha a conta correta para o projeto.",
        "identity_mismatch": "A conta autenticada não corresponde à conta vinculada a este projeto.",
        "identity_conflict": "Esta conta do Instagram já está vinculada a outro projeto.",
        "reconciliation_expired": "A confirmação expirou. Conecte a conta novamente.",
        "reconciliation_required": "Confirme o vínculo da conta antes de continuar.",
        "pagination_limit": "A sincronização excedeu o limite seguro de páginas. Tente novamente mais tarde.",
        "permission_denied": "A conta conectada não concedeu a permissão necessária para esta capacidade.",
        "rate_limited": "A API do Instagram atingiu temporariamente o limite de chamadas. Tente novamente mais tarde.",
        "temporary": "A API do Instagram não respondeu a tempo. Tente novamente mais tarde.",
        "unsupported_metric": "Esta métrica não é disponibilizada pela API para a amostra testada.",
    }
    return messages.get(error.category, "Não foi possível concluir a operação com a API do Instagram.")


def _safe_meta_log_value(value: object, *, limit: int = 80) -> str | None:
    """Normaliza campos diagnósticos sem aceitar quebras ou texto arbitrário."""
    if value is None:
        return None
    normalized = "".join(
        character for character in str(value)
        if character.isalnum() or character in {"_", "-", ".", ":"}
    )
    return normalized[:limit] or None


def log_meta_capability_failure(
    *,
    provider: str,
    probe: str,
    error: InstagramMetaError,
    metric: str | None = None,
    media_type: str | None = None,
) -> None:
    """Registra somente metadados seguros de uma falha de probe Meta.

    O evento deliberadamente não contém mensagem externa, URL, payload nem
    credenciais. Os valores são sanitizados mesmo quando vierem de resposta
    remota antes de chegarem ao logger.
    """
    logging.getLogger(__name__).warning(
        "instagram_meta_capability_failure provider=%s probe=%s metric=%s media_type=%s "
        "http_status=%s meta_code=%s meta_subcode=%s meta_type=%s classification=%s",
        _safe_meta_log_value(provider),
        _safe_meta_log_value(probe),
        _safe_meta_log_value(metric),
        _safe_meta_log_value(media_type),
        error.http_status,
        _safe_meta_log_value(error.meta_code),
        _safe_meta_log_value(error.meta_subcode),
        _safe_meta_log_value(error.meta_type),
        _safe_meta_log_value(error.category),
    )
    # Evita um segundo evento genérico quando run_capability_test propaga a
    # mesma exceção até a rota.
    error._capability_log_emitted = True


@dataclass(frozen=True)
class InstagramMetaSettings:
    facebook_app_id: str
    facebook_app_secret: str
    instagram_app_id: str
    instagram_app_secret: str
    redirect_uri: str
    api_version: str
    token_encryption_key: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "InstagramMetaSettings":
        return cls(
            facebook_app_id=str(values.get("PESQUISAPDF_META_APP_ID") or "").strip(),
            facebook_app_secret=str(values.get("PESQUISAPDF_META_APP_SECRET") or "").strip(),
            instagram_app_id=str(values.get("PESQUISAPDF_META_INSTAGRAM_APP_ID") or "").strip(),
            instagram_app_secret=str(values.get("PESQUISAPDF_META_INSTAGRAM_APP_SECRET") or "").strip(),
            redirect_uri=str(values.get("PESQUISAPDF_META_REDIRECT_URI") or "").strip(),
            api_version=str(values.get("PESQUISAPDF_META_API_VERSION") or "v26.0").strip(),
            token_encryption_key=str(values.get("PESQUISAPDF_META_TOKEN_ENCRYPTION_KEY") or "").strip(),
        )

    @property
    def encryption_available(self) -> bool:
        if not (self.token_encryption_key and Fernet):
            return False
        try:
            Fernet(self.token_encryption_key.encode("ascii"))
        except (TypeError, UnicodeEncodeError, ValueError):
            return False
        return True

    @property
    def common_available(self) -> bool:
        return bool(self.redirect_uri and self.encryption_available)

    @property
    def instagram_login_available(self) -> bool:
        return bool(self.common_available and self.instagram_app_id and self.instagram_app_secret)

    @property
    def facebook_login_available(self) -> bool:
        return bool(self.common_available and self.facebook_app_id and self.facebook_app_secret)

    @property
    def available(self) -> bool:
        """Compatibilidade para chamadas existentes: disponibilidade Facebook."""
        return self.facebook_login_available

    def provider_available(self, provider: str) -> bool:
        if provider == "instagram_login":
            return self.instagram_login_available
        if provider == "facebook_login":
            return self.facebook_login_available
        return False

    def app_credentials(self, provider: str) -> tuple[str, str]:
        if provider == "instagram_login":
            return self.instagram_app_id, self.instagram_app_secret
        if provider == "facebook_login":
            return self.facebook_app_id, self.facebook_app_secret
        raise InstagramMetaConfigurationError("O método de conexão solicitado não é suportado.")


class InstagramTokenCipher:
    """Cifra autenticada Fernet; o valor retornado pode ser guardado no banco."""

    def __init__(self, key: str):
        if not key or Fernet is None:
            raise InstagramMetaConfigurationError()
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError):
            raise InstagramMetaConfigurationError(
                "A chave de criptografia da integração com Instagram é inválida."
            ) from None

    def encrypt(self, value: str) -> str:
        if not value:
            raise InstagramMetaError("unknown", "A autorização não retornou uma credencial utilizável.")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str:
        if not value:
            raise InstagramMetaError("reconnect_required", "Reconecte a conta do Instagram antes de testar capacidades.")
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise InstagramMetaConfigurationError(
                "A credencial armazenada não pode ser lida com a configuração atual. Reconecte a conta."
            ) from None


def _safe_error_category(status: int | None, payload: object, *, operation: str = "generic") -> str:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    if code == 190 or status == 401:
        return "reconnect_required"
    if code in {10, 200, 201, 294} or status == 403:
        return "permission_denied"
    if code in {4, 17, 32, 613} or status == 429:
        return "rate_limited"
    # O código 100 também pode indicar campo/objeto inválido em probes
    # estruturais. Só o tratamos como indisponibilidade parcial quando a
    # requisição era efetivamente de métricas.
    if code == 100 and operation == "metric":
        return "unsupported_metric"
    if status and status >= 500:
        return "temporary"
    return "unknown"


@dataclass(frozen=True)
class FacebookPageCandidate:
    page_id: str
    page_name: str
    page_access_token: str = field(repr=False)
    instagram_profile: Mapping[str, Any]

    @property
    def external_id(self) -> str:
        return str(self.instagram_profile.get("id") or "").strip()

    @property
    def connection_access_token(self) -> str:
        return self.page_access_token


@dataclass(frozen=True)
class InstagramLoginCandidate:
    instagram_access_token: str = field(repr=False)
    instagram_profile: Mapping[str, Any]

    @property
    def external_id(self) -> str:
        return str(self.instagram_profile.get("id") or "").strip()

    @property
    def connection_access_token(self) -> str:
        return self.instagram_access_token

    @property
    def page_name(self) -> str:
        return "Conexão direta"


@dataclass
class _PendingSelection:
    user_id: str
    project_id: str
    provider: str
    expires_at: float
    candidates: tuple[Any, ...]
    granted_scopes: tuple[str, ...]


class MetaConnectionSelections:
    """Seleção efêmera em memória; tokens não entram no cookie de sessão."""

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, _PendingSelection] = {}
        self._lock = RLock()

    def _purge(self, now: float) -> None:
        self._items = {key: value for key, value in self._items.items() if value.expires_at > now}

    def put(
        self,
        user_id: str,
        project_id: str,
        provider: str,
        candidates: list[Any],
        granted_scopes: list[str],
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = monotonic()
        with self._lock:
            self._purge(now)
            self._items[token] = _PendingSelection(
                user_id=str(user_id), project_id=str(project_id), provider=provider,
                expires_at=now + self.ttl_seconds,
                candidates=tuple(candidates), granted_scopes=tuple(granted_scopes),
            )
        return token

    def get(self, token: str, user_id: str, project_id: str) -> _PendingSelection:
        with self._lock:
            self._purge(monotonic())
            selection = self._items.get(token)
            if selection is None or selection.user_id != str(user_id) or selection.project_id != str(project_id):
                raise InstagramMetaError("unknown", "A seleção da conta expirou.")
            return selection

    def claim(
        self, token: str, user_id: str, project_id: str, index: int
    ) -> tuple[Any, tuple[str, ...]]:
        with self._lock:
            selection = self.get(token, user_id, project_id)
            if index < 0 or index >= len(selection.candidates):
                raise InstagramMetaError("unknown", "A conta selecionada não é válida.")
            self._items.pop(token, None)
            return selection.candidates[index], selection.granted_scopes


pending_meta_selections = MetaConnectionSelections()


@dataclass
class _PendingReconciliation:
    user_id: str
    project_id: str
    provider: str
    expires_at: float
    candidate: Any
    granted_scopes: tuple[str, ...]
    account_id: str
    account_external_id: str | None
    account_connection_status: str
    connection_id: str | None


class MetaConnectionReconciliations:
    """Confirmação efêmera, no servidor, para identidades manuais divergentes.

    A credencial recém-autorizada permanece somente no candidato guardado em
    memória até o usuário confirmar. O token exposto na URL é apenas um nonce
    de curta duração e é consumido uma única vez.
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, _PendingReconciliation] = {}
        self._lock = RLock()

    def _purge(self, now: float) -> None:
        self._items = {key: value for key, value in self._items.items() if value.expires_at > now}

    def put(
        self,
        user_id: str,
        project: AnalyticsProject,
        provider: str,
        candidate: Any,
        granted_scopes: list[str] | tuple[str, ...],
    ) -> str:
        account = project.instagram_account
        if account is None:
            raise InstagramMetaError("identity_mismatch", "O projeto não possui a conta do Instagram que seria reconciliada.")
        connection = account.api_connection
        token = secrets.token_urlsafe(32)
        now = monotonic()
        with self._lock:
            self._purge(now)
            self._items[token] = _PendingReconciliation(
                user_id=str(user_id),
                project_id=str(project.id),
                provider=provider,
                expires_at=now + self.ttl_seconds,
                candidate=candidate,
                granted_scopes=tuple(granted_scopes),
                account_id=str(account.id),
                account_external_id=account.external_id,
                account_connection_status=account.connection_status,
                connection_id=str(connection.id) if connection is not None else None,
            )
        return token

    def get(self, token: str, user_id: str, project_id: str) -> _PendingReconciliation:
        with self._lock:
            self._purge(monotonic())
            reconciliation = self._items.get(token)
            if (
                reconciliation is None
                or reconciliation.user_id != str(user_id)
                or reconciliation.project_id != str(project_id)
            ):
                raise InstagramMetaError("reconciliation_expired", "A confirmação do vínculo expirou.")
            return reconciliation

    def claim(self, token: str, user_id: str, project_id: str) -> _PendingReconciliation:
        with self._lock:
            reconciliation = self.get(token, user_id, project_id)
            self._items.pop(token, None)
            return reconciliation


pending_meta_reconciliations = MetaConnectionReconciliations()


class MetaConnectionProvider:
    """Contrato dos fluxos OAuth, sem decisões de provider espalhadas nas rotas."""

    key = ""
    label = ""
    scopes: tuple[str, ...] = ()

    def __init__(self, settings: InstagramMetaSettings):
        if not settings.provider_available(self.key):
            raise InstagramMetaConfigurationError(
                f"A integração {self.label or 'Meta'} não está configurada neste ambiente."
            )
        self.settings = settings

    def sample_comments(self, media_external_id: str, access_token: str) -> list[dict[str, Any]]:
        """Lê uma página limitada de comentários para um probe não persistente."""
        raise NotImplementedError

    def sample_comment_replies(self, comment_external_id: str, access_token: str) -> list[dict[str, Any]]:
        """Contrato da futura sincronização: respostas também pertencem ao provider."""
        raise NotImplementedError

    def list_media(self, account_external_id: str, access_token: str) -> list[dict[str, Any]]:
        """Lê todas as mídias com paginação limitada defensivamente para sync."""
        raise NotImplementedError


class FacebookLoginProvider(MetaConnectionProvider):
    """Facebook Login + Graph API com User Token efêmero e Page Token cifrado."""

    key = "facebook_login"
    label = "Facebook Login"
    scopes = FACEBOOK_LOGIN_SCOPES

    def authorization_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.settings.facebook_app_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "scope": ",".join(self.scopes),
            "state": state,
        })
        return f"https://www.facebook.com/{self.settings.api_version}/dialog/oauth?{query}"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        access_token: str | None = None,
        form: Mapping[str, str] | None = None,
        operation: str = "generic",
    ) -> dict[str, Any]:
        data = urlencode(form).encode("utf-8") if form is not None else None
        headers = {"Accept": "application/json", "User-Agent": "Analysis-Instagram-Capability/1.0"}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:  # nosec B310 - URL é constante deste módulo
                raw = response.read(1_000_000)
        except HTTPError as error:
            raw = error.read(8_192)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            meta_error = payload.get("error", {}) if isinstance(payload, Mapping) else {}
            meta_error = meta_error if isinstance(meta_error, Mapping) else {}
            raise InstagramMetaError(
                _safe_error_category(error.code, payload, operation=operation),
                "A Graph API recusou esta operação.",
                http_status=error.code,
                meta_code=meta_error.get("code"),
                meta_subcode=meta_error.get("error_subcode"),
                meta_type=_safe_meta_log_value(meta_error.get("type")),
            ) from None
        except (URLError, TimeoutError, OSError):
            raise InstagramMetaError("temporary", "A Graph API não respondeu a tempo.") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InstagramMetaError("unknown", "A Graph API retornou uma resposta inválida.") from None
        if not isinstance(payload, dict):
            raise InstagramMetaError("unknown", "A Graph API retornou uma resposta inesperada.")
        return payload

    def exchange_code(self, code: str) -> dict[str, Any]:
        query = urlencode({
            "client_id": self.settings.facebook_app_id,
            "redirect_uri": self.settings.redirect_uri,
            "client_secret": self.settings.facebook_app_secret,
            "code": code,
        })
        payload = self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/oauth/access_token?{query}"
        )
        if not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise InstagramMetaError("unknown", "A autorização do Facebook não retornou uma credencial utilizável.")
        return payload

    @staticmethod
    def _page_fields() -> str:
        return "id,name,access_token,instagram_business_account{id,username,name,followers_count,media_count}"

    def _managed_pages(self, user_access_token: str) -> list[Mapping[str, Any]]:
        after: str | None = None
        pages: list[Mapping[str, Any]] = []
        for _ in range(PAGE_DISCOVERY_MAX_PAGES):
            query: dict[str, str] = {"fields": self._page_fields(), "limit": str(PAGE_DISCOVERY_LIMIT)}
            if after:
                query["after"] = after
            payload = self._request_json(
                "GET", f"https://graph.facebook.com/{self.settings.api_version}/me/accounts?{urlencode(query)}",
                access_token=user_access_token,
            )
            rows = payload.get("data", [])
            if not isinstance(rows, list):
                raise InstagramMetaError("unknown", "A Graph API retornou páginas em formato inesperado.")
            pages.extend(row for row in rows if isinstance(row, Mapping))
            paging = payload.get("paging")
            cursors = paging.get("cursors", {}) if isinstance(paging, Mapping) else {}
            next_after = cursors.get("after") if isinstance(cursors, Mapping) else None
            if not isinstance(next_after, str) or not next_after or next_after == after:
                break
            after = next_after
        return pages

    def _debug_user_token(self, user_access_token: str) -> Mapping[str, Any]:
        """Lê a autorização granular no servidor, sem persistir ou expor tokens."""
        query = urlencode({"input_token": user_access_token})
        app_access_token = f"{self.settings.facebook_app_id}|{self.settings.facebook_app_secret}"
        payload = self._request_json(
            "GET",
            f"https://graph.facebook.com/{self.settings.api_version}/debug_token?{query}",
            access_token=app_access_token,
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise InstagramMetaError("unknown", "A Graph API não retornou a introspecção esperada do token.")
        return data

    def _authorized_page_target_ids(self, user_access_token: str) -> list[str]:
        data = self._debug_user_token(user_access_token)
        if data.get("is_valid") is not True:
            raise InstagramMetaError("reconnect_required", "A autorização do Facebook não é mais válida.")
        if str(data.get("app_id") or "") != self.settings.facebook_app_id:
            return []

        page_scopes = {"pages_show_list", "pages_read_engagement"}
        target_ids: list[str] = []
        seen: set[str] = set()
        granular_scopes = data.get("granular_scopes", [])
        if not isinstance(granular_scopes, list):
            return target_ids
        # A união preserva Pages autorizadas por apenas um dos dois escopos. A
        # consulta direta abaixo continua sendo a validação efetiva do vínculo.
        for scope in granular_scopes:
            if not isinstance(scope, Mapping) or str(scope.get("scope") or "") not in page_scopes:
                continue
            raw_target_ids = scope.get("target_ids", [])
            if not isinstance(raw_target_ids, list):
                continue
            for raw_target_id in raw_target_ids:
                target_id = str(raw_target_id or "").strip()
                if target_id and target_id not in seen:
                    seen.add(target_id)
                    target_ids.append(target_id)
                    if len(target_ids) >= PAGE_DISCOVERY_FALLBACK_LIMIT:
                        return target_ids
        return target_ids

    def _authorized_page(self, page_id: str, user_access_token: str) -> Mapping[str, Any] | None:
        query = urlencode({"fields": self._page_fields()})
        payload = self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{page_id}?{query}",
            access_token=user_access_token,
        )
        return payload if isinstance(payload, Mapping) else None

    @staticmethod
    def _page_candidates(pages: list[Mapping[str, Any]]) -> list[FacebookPageCandidate]:
        candidates: list[FacebookPageCandidate] = []
        page_ids: set[str] = set()
        instagram_account_ids: set[str] = set()
        for page in pages:
            profile = page.get("instagram_business_account")
            page_token = page.get("access_token")
            page_id = str(page.get("id") or "").strip()
            if page_id in page_ids:
                continue
            page_ids.add(page_id)
            if not isinstance(profile, Mapping) or not isinstance(page_token, str) or not page_token or not page_id:
                continue
            try:
                instagram_account_id, _, _, _ = profile_identity(profile)
            except InstagramMetaError:
                continue
            if instagram_account_id in instagram_account_ids:
                continue
            instagram_account_ids.add(instagram_account_id)
            candidates.append(FacebookPageCandidate(
                page_id=page_id,
                page_name=str(page.get("name") or "Página sem nome").strip() or "Página sem nome",
                page_access_token=page_token,
                instagram_profile=dict(profile),
            ))
        return candidates

    def discover_professional_accounts(self, user_access_token: str) -> list[FacebookPageCandidate]:
        """Descobre Pages pela listagem principal e por autorização granular limitada."""
        pages = self._managed_pages(user_access_token)
        primary_candidates = self._page_candidates(pages)
        try:
            fallback_page_ids = self._authorized_page_target_ids(user_access_token)
        except InstagramMetaError as error:
            # O fallback não pode invalidar candidatos completos do caminho principal.
            if primary_candidates and error.category != "reconnect_required":
                return primary_candidates
            raise

        listed_page_ids = {
            str(page.get("id") or "").strip()
            for page in pages
            if str(page.get("id") or "").strip()
        }
        for page_id in fallback_page_ids:
            if page_id in listed_page_ids:
                continue
            try:
                page = self._authorized_page(page_id, user_access_token)
            except InstagramMetaError:
                # Falhas de uma Page específica não impedem as demais candidatas.
                continue
            if page is not None:
                pages.append(page)
                listed_page_ids.add(page_id)

        candidates = self._page_candidates(pages)
        if not candidates:
            raise InstagramMetaError("no_linked_instagram_account", "Nenhuma Página possui conta profissional vinculada.")
        return candidates

    def resolve_connection_candidates(self, user_access_token: str) -> list[FacebookPageCandidate]:
        return self.discover_professional_accounts(user_access_token)

    def instagram_profile(self, instagram_account_id: str, page_access_token: str) -> dict[str, Any]:
        # O IG User do Graph API via Facebook Login aceita a validação mínima
        # de identidade. Campos como name/account_type são enriquecimento, não
        # pré-requisito para reconhecer a conta, e não podem interromper o
        # capability quando a versão da API não os disponibiliza.
        query = urlencode({"fields": "id,username"})
        return self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{instagram_account_id}?{query}",
            access_token=page_access_token,
            operation="profile_identity",
        )

    def instagram_profile_enrichment(self, instagram_account_id: str, page_access_token: str) -> dict[str, Any]:
        """Lê contagens opcionais sem ampliar o contrato de identidade mínimo."""
        query = urlencode({"fields": "followers_count"})
        return self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{instagram_account_id}?{query}",
            access_token=page_access_token,
            operation="profile_enrichment",
        )

    def _list_media_from_graph(
        self,
        graph_host: str,
        account_external_id: str,
        access_token: str,
    ) -> list[dict[str, Any]]:
        """Paginação por cursor, nunca por URL devolvida pela API.

        O limite é explícito: se ele for alcançado com cursor pendente, a
        sincronização falha sem persistir uma coleção silenciosamente truncada.
        """
        after: str | None = None
        seen_cursors: set[str] = set()
        media: list[dict[str, Any]] = []
        for _ in range(SYNC_MEDIA_MAX_PAGES):
            query: dict[str, str] = {
                "fields": SYNC_MEDIA_FIELDS,
                "limit": str(SYNC_MEDIA_PAGE_LIMIT),
            }
            if after is not None:
                query["after"] = after
            payload = self._request_json(
                "GET",
                f"{graph_host}/{self.settings.api_version}/{account_external_id}/media?{urlencode(query)}",
                access_token=access_token,
                operation="media_sync",
            )
            rows = payload.get("data", [])
            if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                raise InstagramMetaError("payload_invalid", "A API retornou mídias em formato inesperado.")
            media.extend(dict(row) for row in rows)
            paging = payload.get("paging")
            cursors = paging.get("cursors", {}) if isinstance(paging, Mapping) else {}
            next_after = cursors.get("after") if isinstance(cursors, Mapping) else None
            if not isinstance(next_after, str) or not next_after:
                return media
            if next_after in seen_cursors or next_after == after:
                raise InstagramMetaError("payload_invalid", "A paginação de mídias retornou um cursor inválido.")
            seen_cursors.add(next_after)
            after = next_after
        raise InstagramMetaError("pagination_limit", "A paginação de mídias excedeu o limite seguro.")

    def list_media(self, account_external_id: str, page_access_token: str) -> list[dict[str, Any]]:
        return self._list_media_from_graph(
            "https://graph.facebook.com", account_external_id, page_access_token
        )

    def sample_media(self, account_external_id: str, page_access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"fields": "id,media_type,media_product_type", "limit": str(CAPABILITY_MEDIA_LIMIT)})
        payload = self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{account_external_id}/media?{query}",
            access_token=page_access_token,
            operation="media_sample",
        )
        rows = payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)][:CAPABILITY_MEDIA_LIMIT] if isinstance(rows, list) else []

    def sample_comments(self, media_external_id: str, page_access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"fields": "id", "limit": str(CAPABILITY_COMMENTS_PER_MEDIA_LIMIT)})
        payload = self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{media_external_id}/comments?{query}",
            access_token=page_access_token,
            operation="media_comments",
        )
        rows = payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)][:CAPABILITY_COMMENTS_PER_MEDIA_LIMIT] if isinstance(rows, list) else []

    def sample_comment_replies(self, comment_external_id: str, page_access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"fields": "id", "limit": str(CAPABILITY_COMMENTS_PER_MEDIA_LIMIT)})
        payload = self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{comment_external_id}/replies?{query}",
            access_token=page_access_token,
        )
        rows = payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)][:CAPABILITY_COMMENTS_PER_MEDIA_LIMIT] if isinstance(rows, list) else []

    def media_metric(self, media_external_id: str, metric: str, page_access_token: str) -> dict[str, Any]:
        return self.media_metrics(media_external_id, (metric,), page_access_token)

    def media_metrics(
        self, media_external_id: str, metrics: tuple[str, ...], page_access_token: str
    ) -> dict[str, Any]:
        """Consulta métricas compatíveis em lote para reduzir chamadas da sincronização.

        A API aceita a lista separada por vírgulas. O chamador continua
        responsável por só agrupar métricas já comprovadas para o mesmo tipo de
        mídia, pois uma métrica incompatível pode invalidar a requisição inteira.
        """
        requested = tuple(metric.strip() for metric in metrics if isinstance(metric, str) and metric.strip())
        if not requested:
            raise InstagramMetaError("payload_invalid", "Nenhuma métrica foi solicitada.")
        query = urlencode({"metric": ",".join(requested)})
        return self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{media_external_id}/insights?{query}",
            access_token=page_access_token,
            operation="metric",
        )

    def account_insights(self, instagram_account_id: str, page_access_token: str) -> dict[str, Any]:
        query = urlencode({"metric": "reach", "period": "day"})
        return self._request_json(
            "GET", f"https://graph.facebook.com/{self.settings.api_version}/{instagram_account_id}/insights?{query}",
            access_token=page_access_token,
            operation="metric",
        )


class InstagramLoginProvider(FacebookLoginProvider):
    """Instagram Login direto, sem Página ou credencial Facebook persistida."""

    key = "instagram_login"
    label = "Instagram Login"
    scopes = INSTAGRAM_LOGIN_SCOPES

    def authorization_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.settings.instagram_app_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "scope": ",".join(self.scopes),
            "state": state,
        })
        return f"https://www.instagram.com/oauth/authorize?{query}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            "https://api.instagram.com/oauth/access_token",
            form={
                "client_id": self.settings.instagram_app_id,
                "client_secret": self.settings.instagram_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.redirect_uri,
                "code": code,
            },
        )
        if not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise InstagramMetaError("unknown", "A autorização do Instagram não retornou uma credencial utilizável.")
        return payload

    def resolve_authenticated_profile(self, instagram_access_token: str) -> dict[str, Any]:
        query = urlencode({"fields": "id,username,name,account_type,media_count"})
        profile = self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/me?{query}",
            access_token=instagram_access_token,
        )
        profile_identity(profile)
        return profile

    def resolve_connection_candidates(self, instagram_access_token: str) -> list[InstagramLoginCandidate]:
        return [InstagramLoginCandidate(
            instagram_access_token=instagram_access_token,
            instagram_profile=self.resolve_authenticated_profile(instagram_access_token),
        )]

    def instagram_profile(self, instagram_account_id: str, instagram_access_token: str) -> dict[str, Any]:
        query = urlencode({"fields": "id,username,name,account_type,media_count"})
        return self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{instagram_account_id}?{query}",
            access_token=instagram_access_token,
        )

    def instagram_profile_enrichment(self, instagram_account_id: str, instagram_access_token: str) -> dict[str, Any]:
        """Mantém seguidores separados da identidade usada no OAuth direto."""
        query = urlencode({"fields": "followers_count"})
        return self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{instagram_account_id}?{query}",
            access_token=instagram_access_token,
            operation="profile_enrichment",
        )

    def list_media(self, account_external_id: str, instagram_access_token: str) -> list[dict[str, Any]]:
        return self._list_media_from_graph(
            "https://graph.instagram.com", account_external_id, instagram_access_token
        )

    def sample_media(self, account_external_id: str, instagram_access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"fields": "id,media_type,media_product_type", "limit": str(CAPABILITY_MEDIA_LIMIT)})
        payload = self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{account_external_id}/media?{query}",
            access_token=instagram_access_token,
        )
        rows = payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)][:CAPABILITY_MEDIA_LIMIT] if isinstance(rows, list) else []

    def sample_comments(self, media_external_id: str, instagram_access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"fields": "id", "limit": str(CAPABILITY_COMMENTS_PER_MEDIA_LIMIT)})
        payload = self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{media_external_id}/comments?{query}",
            access_token=instagram_access_token,
        )
        rows = payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)][:CAPABILITY_COMMENTS_PER_MEDIA_LIMIT] if isinstance(rows, list) else []

    def sample_comment_replies(self, comment_external_id: str, instagram_access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"fields": "id", "limit": str(CAPABILITY_COMMENTS_PER_MEDIA_LIMIT)})
        payload = self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{comment_external_id}/replies?{query}",
            access_token=instagram_access_token,
        )
        rows = payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)][:CAPABILITY_COMMENTS_PER_MEDIA_LIMIT] if isinstance(rows, list) else []

    def media_metrics(
        self, media_external_id: str, metrics: tuple[str, ...], instagram_access_token: str
    ) -> dict[str, Any]:
        """Consulta Insights no host do Instagram Login, não no Graph do Facebook."""
        requested = tuple(metric.strip() for metric in metrics if isinstance(metric, str) and metric.strip())
        if not requested:
            raise InstagramMetaError("payload_invalid", "Nenhuma métrica foi solicitada.")
        query = urlencode({"metric": ",".join(requested)})
        return self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{media_external_id}/insights?{query}",
            access_token=instagram_access_token,
            operation="metric",
        )

    def account_insights(self, instagram_account_id: str, instagram_access_token: str) -> dict[str, Any]:
        query = urlencode({"metric": "reach", "period": "day"})
        return self._request_json(
            "GET", f"https://graph.instagram.com/{self.settings.api_version}/{instagram_account_id}/insights?{query}",
            access_token=instagram_access_token,
            operation="metric",
        )


def connection_provider(settings: InstagramMetaSettings, provider: str) -> MetaConnectionProvider:
    providers = {
        "instagram_login": InstagramLoginProvider,
        "facebook_login": FacebookLoginProvider,
    }
    try:
        return providers[provider](settings)
    except KeyError:
        raise InstagramMetaConfigurationError("O método de conexão solicitado não é suportado.") from None


def profile_identity(profile: Mapping[str, Any]) -> tuple[str, str, str, str | None]:
    external_id = str(profile.get("id") or "").strip()
    raw_username = str(profile.get("username") or "")
    account_type = str(profile.get("account_type") or "").strip() or None
    if not external_id:
        raise InstagramMetaError("no_linked_instagram_account", "A Graph API não retornou a identidade Instagram necessária.")
    try:
        username, normalized_username = normalized_instagram_username(raw_username)
    except InstagramImportError:
        raise InstagramMetaError("no_linked_instagram_account", "A Graph API não retornou username da conta profissional.") from None
    return external_id, username, normalized_username, account_type


def _safe_scopes(token_payload: Mapping[str, Any]) -> list[str]:
    raw = token_payload.get("scope", token_payload.get("scopes", []))
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, (list, tuple)):
        return list(FACEBOOK_LOGIN_SCOPES)
    granted = {str(scope) for scope in raw}
    return [scope for scope in FACEBOOK_LOGIN_SCOPES if not granted or scope in granted]


def provider_scopes(provider: str) -> tuple[str, ...]:
    if provider == "instagram_login":
        return INSTAGRAM_LOGIN_SCOPES
    if provider == "facebook_login":
        return FACEBOOK_LOGIN_SCOPES
    raise InstagramMetaError("configuration", "O método de conexão solicitado não é suportado.")


def choose_candidate(
    project: AnalyticsProject, candidates: list[Any]
) -> Any | None:
    """Resolve identidade existente; ``None`` exige uma escolha explícita."""
    account = project.instagram_account
    connection = account.api_connection if account is not None else None
    provider_external_id = connection.provider_account_external_id if connection is not None else None
    if provider_external_id:
        matches = [candidate for candidate in candidates if candidate.external_id == provider_external_id]
        if not matches:
            raise InstagramMetaError("identity_mismatch", "Nenhuma conta resolvida corresponde à conexão deste projeto.")
        return matches[0]
    if account is not None and account.external_id:
        matches = [candidate for candidate in candidates if candidate.external_id == account.external_id]
        if matches:
            return matches[0]
    identities: dict[str, Any] = {}
    for candidate in candidates:
        identities.setdefault(candidate.external_id, candidate)
    return next(iter(identities.values())) if len(identities) == 1 else None


def reconciliation_required(project: AnalyticsProject, profile: Mapping[str, Any]) -> bool:
    """Indica se uma identidade manual precisa de confirmação humana para vincular a API."""
    provider_external_id, _, provider_username_normalized, _ = profile_identity(profile)
    account = project.instagram_account
    if account is None or not account.external_id or account.external_id == provider_external_id:
        return False

    connection = account.api_connection
    if connection is not None and connection.provider_account_external_id:
        if connection.provider_account_external_id != provider_external_id:
            raise InstagramMetaError("identity_mismatch", "A conta autenticada não corresponde à conexão deste projeto.")
        # Uma ligação previamente reconciliada já tem consentimento explícito
        # para manter o ID manual do projeto separado do ID oficial Meta. Uma
        # conexão legada marcada como ``exact`` não tem esse consentimento e
        # precisa passar pela confirmação abaixo.
        if connection.identity_binding == "reconciled":
            return False

    account_using_provider_identity = db.session.scalar(
        select(InstagramAccount).where(
            InstagramAccount.external_id == provider_external_id,
            InstagramAccount.analytics_project_id != project.id,
        )
    )
    provider_connection_elsewhere = db.session.scalar(
        select(InstagramApiConnection)
        .join(InstagramAccount)
        .where(
            InstagramApiConnection.provider_account_external_id == provider_external_id,
            InstagramAccount.analytics_project_id != project.id,
        )
    )
    if account_using_provider_identity is not None or provider_connection_elsewhere is not None:
        raise InstagramMetaError("identity_conflict", "Esta conta do Instagram já está vinculada a outro projeto.")
    if account.username_normalized != provider_username_normalized:
        raise InstagramMetaError("identity_mismatch", "A conta autenticada não corresponde à conta deste projeto.")
    return True


def persist_validated_connection(
    project: AnalyticsProject,
    *,
    access_token: str | None = None,
    page_access_token: str | None = None,
    profile: Mapping[str, Any],
    granted_scopes: tuple[str, ...] | list[str],
    settings: InstagramMetaSettings,
    provider: str = "facebook_login",
    reconciliation_confirmed: bool = False,
) -> InstagramApiConnection:
    """Persiste só a credencial mínima do provider após validar a identidade."""
    if provider not in META_CONNECTION_PROVIDERS:
        raise InstagramMetaError("configuration", "O método de conexão solicitado não é suportado.")
    credential = access_token if access_token is not None else page_access_token
    if not credential:
        raise InstagramMetaError("unknown", "A autorização não retornou uma credencial utilizável.")
    external_id, username, normalized_username, account_type = profile_identity(profile)
    account = project.instagram_account
    occupied = db.session.scalar(select(InstagramAccount).where(
        InstagramAccount.external_id == external_id,
        InstagramAccount.analytics_project_id != project.id,
    ))
    if occupied is not None:
        raise InstagramMetaError("identity_conflict", "Esta conta do Instagram já está vinculada a outro projeto.")
    connection = account.api_connection if account is not None else None
    provider_connection_elsewhere = db.session.scalar(
        select(InstagramApiConnection)
        .join(InstagramAccount)
        .where(
            InstagramApiConnection.provider_account_external_id == external_id,
            InstagramAccount.analytics_project_id != project.id,
        )
    )
    if provider_connection_elsewhere is not None:
        raise InstagramMetaError("identity_conflict", "Esta conta do Instagram já está vinculada a outro projeto.")
    has_existing_provider_identity = bool(
        connection is not None and connection.provider_account_external_id == external_id
    )
    if connection is not None and connection.provider_account_external_id:
        if not has_existing_provider_identity:
            raise InstagramMetaError("identity_mismatch", "A conta autorizada não corresponde à conexão deste projeto.")

    reconciled = False
    if account is not None and account.external_id and account.external_id != external_id:
        if has_existing_provider_identity:
            # O ID oficial já foi confirmado anteriormente; username é mutável
            # e continua sendo atualizado sem tocar na identidade manual. Uma
            # conexão legada marcada como exact, porém, não substitui a
            # confirmação explícita exigida para preservar um ID manual.
            reconciled = connection.identity_binding == "reconciled"
            if not reconciled:
                if account.username_normalized != normalized_username:
                    raise InstagramMetaError("identity_mismatch", "A conta autorizada não corresponde à conta já vinculada a este projeto.")
                if not reconciliation_confirmed:
                    raise InstagramMetaError("reconciliation_required", "Confirme o vínculo da conta antes de continuar.")
                reconciled = True
        else:
            if account.username_normalized != normalized_username:
                raise InstagramMetaError("identity_mismatch", "A conta autorizada não corresponde à conta já vinculada a este projeto.")
            if not reconciliation_confirmed:
                raise InstagramMetaError("reconciliation_required", "Confirme o vínculo da conta antes de continuar.")
            reconciled = True
    cipher = InstagramTokenCipher(settings.token_encryption_key)
    encrypted_access_token = cipher.encrypt(credential)
    now = utcnow()
    if account is None:
        account = InstagramAccount(
            analytics_project=project,
            username=username,
            username_normalized=normalized_username,
            external_id=external_id,
            display_name=str(profile.get("name") or ""),
            account_type=account_type,
            connection_status="connected",
        )
        db.session.add(account)
        db.session.flush()
    else:
        # A identidade estável foi conferida acima. Somente então atualizamos metadados mutáveis.
        if account.external_id is None:
            account.external_id = external_id
        account.username = username
        account.username_normalized = normalized_username
        if profile.get("name"):
            account.display_name = str(profile["name"])
        if account_type:
            account.account_type = account_type
        account.connection_status = "connected"
    if connection is None:
        connection = InstagramApiConnection(
            instagram_account=account,
            provider=provider,
            status="connected",
            api_version=settings.api_version,
        )
        db.session.add(connection)
    provider_changed = connection.provider != provider
    connection.provider = provider
    connection.provider_account_external_id = external_id
    connection.provider_username = username
    connection.identity_binding = "reconciled" if reconciled else "exact"
    connection.status = "connected"
    connection.encrypted_access_token = encrypted_access_token
    connection.encrypted_refresh_token = None
    # O ciclo de vida de Page Access Token não é inferido do User Token efêmero.
    connection.token_expires_at = None
    connection.scopes_json = [scope for scope in provider_scopes(provider) if scope in set(granted_scopes)]
    connection.connected_at = now
    connection.last_validated_at = now
    connection.last_error_category = None
    connection.last_error_at = None
    connection.api_version = settings.api_version
    if provider_changed:
        # O diagnóstico anterior descreve outro método e não pode ser exibido como atual.
        connection.capability_result_json = {}
        connection.last_capability_test_at = None
    db.session.flush()
    return connection


def _media_type(row: Mapping[str, Any]) -> str:
    value = str(row.get("media_product_type") or row.get("media_type") or "unknown").upper()
    if "REEL" in value:
        return "reel"
    if "CAROUSEL" in value:
        return "carousel_album"
    if "VIDEO" in value:
        return "video"
    if "IMAGE" in value:
        return "image"
    return value.casefold() or "unknown"


_PARTIAL_METRIC_ERROR_CATEGORIES = frozenset({
    "permission_denied",
    "unsupported_metric",
    "not_applicable",
    "rate_limited",
    "temporary",
    "temporary_error",
})
_STRUCTURAL_CAPABILITY_ERROR_CATEGORIES = frozenset({
    "reconnect_required",
    "auth_expired",
    "identity_mismatch",
    "configuration",
})


def _raise_if_structural_capability_error(error: InstagramMetaError) -> None:
    """Mantém autenticação, identidade e configuração como falhas globais."""
    if error.category in _STRUCTURAL_CAPABILITY_ERROR_CATEGORIES:
        raise error


def _metric_error_status(error: InstagramMetaError) -> str:
    """Converte apenas respostas Meta esperadas em resultado parcial.

    Erros desconhecidos não são silenciosamente apresentados como uma métrica
    ausente: eles devem continuar visíveis para diagnóstico e correção.
    """
    _raise_if_structural_capability_error(error)
    if error.category not in _PARTIAL_METRIC_ERROR_CATEGORIES:
        raise error
    statuses = {
        "permission_denied": "permission_denied",
        "unsupported_metric": "unsupported",
        "not_applicable": "not_applicable",
        "rate_limited": "temporary_error",
        "temporary": "temporary_error",
        "temporary_error": "temporary_error",
    }
    return statuses[error.category]


def _comments_error_status(error: InstagramMetaError) -> str:
    """Traduz apenas o resultado agregado do probe; nunca a resposta bruta."""
    _raise_if_structural_capability_error(error)
    statuses = {
        "permission_denied": "permission_denied",
        "unsupported_metric": "not_available_for_provider",
        "not_available_for_provider": "not_available_for_provider",
        "rate_limited": "temporary_error",
        "temporary": "temporary_error",
        "temporary_error": "temporary_error",
    }
    return statuses.get(error.category, "unknown")


def _aggregate_metric_status(statuses: list[str]) -> str:
    """Agrega probes sem converter uma falha parcial em falha global."""
    if not statuses:
        return "not_observed"
    if "supported" in statuses:
        return "supported"
    # A ordem dá precedência à informação mais acionável, sem depender da
    # ordenação alfabética de strings internas.
    priority = ("permission_denied", "temporary_error", "not_applicable", "unsupported")
    return next((status for status in priority if status in statuses), statuses[0])


def _media_samples_by_type(media: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Mantém uma amostra curta por tipo, permitindo uma segunda tentativa."""
    samples: dict[str, list[str]] = {}
    for row in media:
        external_media_id = str(row.get("id") or "").strip()
        if not external_media_id:
            continue
        media_type = _media_type(row)
        rows_for_type = samples.setdefault(media_type, [])
        if len(rows_for_type) < CAPABILITY_METRIC_SAMPLES_PER_TYPE_LIMIT:
            rows_for_type.append(external_media_id)
    return samples


def _probe_media_metric(
    client: Any,
    samples_by_type: Mapping[str, list[str]],
    metric: str,
    access_token: str,
    *,
    provider: str,
) -> dict[str, Any]:
    """Testa cada métrica independentemente em até duas mídias por tipo."""
    type_results: dict[str, str] = {}
    temporal: dict[str, dict[str, str]] = {}
    for media_type, media_ids in samples_by_type.items():
        probe_statuses: list[str] = []
        for media_external_id in media_ids:
            try:
                response = client.media_metric(media_external_id, metric, access_token)
            except InstagramMetaError as error:
                log_meta_capability_failure(
                    provider=provider,
                    probe="media_metric",
                    metric=metric,
                    media_type=media_type,
                    error=error,
                )
                probe_statuses.append(_metric_error_status(error))
                continue
            type_results[media_type] = "supported"
            temporal[media_type] = _temporal_metadata(response)
            break
        else:
            type_results[media_type] = _aggregate_metric_status(probe_statuses)
    statuses = list(type_results.values())
    return {
        "status": _aggregate_metric_status(statuses),
        "by_media_type": type_results,
        "temporal_by_media_type": temporal,
    }


def _sample_comments_capability(
    media: list[Mapping[str, Any]], client: Any, access_token: str, *, provider: str
) -> dict[str, Any]:
    """Conta uma amostra curta de comentários sem guardar conteúdo ou identificadores."""
    sampled_media = [
        (str(row.get("id") or "").strip(), _media_type(row))
        for row in media
        if str(row.get("id") or "").strip()
    ][:CAPABILITY_COMMENT_MEDIA_LIMIT]
    if not sampled_media:
        return {"status": "not_observed", "observed_count": None, "sampled_media_count": 0}

    observed_count = 0
    successful_probes = 0
    statuses: list[str] = []
    for media_external_id, media_type in sampled_media:
        try:
            comments = client.sample_comments(media_external_id, access_token)
        except (AttributeError, NotImplementedError):
            statuses.append("not_available_for_provider")
            continue
        except InstagramMetaError as error:
            log_meta_capability_failure(
                provider=provider,
                probe="media_comments",
                media_type=media_type,
                error=error,
            )
            statuses.append(_comments_error_status(error))
            continue
        successful_probes += 1
        observed_count += len(comments)

    if successful_probes:
        return {
            "status": "supported",
            "observed_count": observed_count,
            "sampled_media_count": len(sampled_media),
            "successful_probe_count": successful_probes,
        }

    priority = (
        "permission_denied",
        "requires_reauth",
        "not_available_for_provider",
        "temporary_error",
        "unknown",
    )
    status = next((candidate for candidate in priority if candidate in statuses), "unknown")
    return {
        "status": status,
        "observed_count": None,
        "sampled_media_count": len(sampled_media),
        "successful_probe_count": 0,
    }


def _temporal_metadata(payload: Mapping[str, Any]) -> dict[str, str]:
    rows = payload.get("data", [])
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else {}
    period = str(row.get("period") or "").strip()
    values = row.get("values", [])
    value = values[-1] if isinstance(values, list) and values and isinstance(values[-1], Mapping) else {}
    end_time = str(value.get("end_time") or row.get("end_time") or "").strip()
    if period.casefold() in {"lifetime", "total_value"}:
        semantics = "lifetime"
    elif period:
        semantics = "periodized"
    else:
        semantics = "unknown"
    result = {"temporal_semantics": semantics}
    if period:
        result["period"] = period
    if end_time:
        result["end_time"] = end_time
    return result


def _profile_field_status(profile: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: "supported" if profile.get(field) is not None else "unknown"
        for field in ("id", "username", "name", "followers_count", "media_count")
    }


def _run_facebook_capability_test(
    connection: InstagramApiConnection,
    settings: InstagramMetaSettings,
    client: Any,
) -> dict[str, Any]:
    """Consulta probes mínimos na Graph API, sem sincronizar dados canônicos.

    O resultado é seguro para persistência: não contém token, cabeçalho, URL de
    autorização ou corpo bruto retornado por Meta.
    """
    if not settings.facebook_login_available:
        raise InstagramMetaConfigurationError()
    started = monotonic()
    page_token = InstagramTokenCipher(settings.token_encryption_key).decrypt(connection.encrypted_access_token)
    now = utcnow()
    try:
        provider_account_id = (
            connection.provider_account_external_id or connection.instagram_account.external_id
        )
        if not provider_account_id:
            error = InstagramMetaError("identity_mismatch", "A conexão não possui uma identidade oficial da conta.")
            log_meta_capability_failure(provider="facebook_login", probe="profile_identity", error=error)
            raise error
        try:
            profile = client.instagram_profile(provider_account_id, page_token)
            external_id, _, _, _ = profile_identity(profile)
        except InstagramMetaError as error:
            log_meta_capability_failure(
                provider="facebook_login", probe="profile_identity", error=error,
            )
            raise
        if external_id != provider_account_id:
            error = InstagramMetaError("identity_mismatch", "A credencial não corresponde à conta vinculada ao projeto.")
            log_meta_capability_failure(provider="facebook_login", probe="profile_identity", error=error)
            raise error
        try:
            media = client.sample_media(external_id, page_token)
        except InstagramMetaError as error:
            log_meta_capability_failure(provider="facebook_login", probe="media_sample", error=error)
            raise
        comments = _sample_comments_capability(media, client, page_token, provider="facebook_login")
        samples_by_type = _media_samples_by_type(media)
        metrics = {
            metric: _probe_media_metric(
                client, samples_by_type, metric, page_token, provider="facebook_login"
            )
            for metric in CAPABILITY_METRICS
        }
        try:
            account_payload = client.account_insights(external_id, page_token)
            account_metrics = {"status": "supported", **_temporal_metadata(account_payload)}
        except InstagramMetaError as error:
            log_meta_capability_failure(
                provider="facebook_login", probe="account_insights", metric="reach", error=error,
            )
            account_metrics = {"status": _metric_error_status(error), "temporal_semantics": "unknown"}
        result = {
            "checked_at": now.isoformat(),
            "api_version": settings.api_version,
            "provider": "facebook_login",
            "profile": {"status": "supported", "fields": _profile_field_status(profile)},
            "sample": {
                "status": "supported",
                "limit": CAPABILITY_MEDIA_LIMIT,
                "observed_count": len(media),
                "media_types": sorted(samples_by_type),
            },
            "comments": comments,
            "media_metrics": metrics,
            "account_metrics": account_metrics,
            "duration_ms": round((monotonic() - started) * 1000),
        }
    except InstagramMetaError as error:
        connection.status = "needs_reauth" if error.category in {"reconnect_required", "auth_expired"} else "error"
        connection.instagram_account.connection_status = connection.status
        connection.last_error_category = error.category
        connection.last_error_at = now
        db.session.flush()
        raise
    connection.status = "connected"
    connection.instagram_account.connection_status = "connected"
    connection.api_version = settings.api_version
    connection.capability_result_json = result
    connection.last_capability_test_at = now
    connection.last_error_category = None
    connection.last_error_at = None
    db.session.flush()
    return result


def _run_instagram_capability_test(
    connection: InstagramApiConnection,
    settings: InstagramMetaSettings,
    client: Any,
) -> dict[str, Any]:
    """Diagnóstico direto com probes de Insights isolados por métrica."""
    if not settings.instagram_login_available:
        raise InstagramMetaConfigurationError("A integração Instagram Login não está configurada neste ambiente.")
    started = monotonic()
    instagram_token = InstagramTokenCipher(settings.token_encryption_key).decrypt(connection.encrypted_access_token)
    now = utcnow()
    try:
        provider_account_id = (
            connection.provider_account_external_id or connection.instagram_account.external_id
        )
        if not provider_account_id:
            error = InstagramMetaError("identity_mismatch", "A conexão não possui uma identidade oficial da conta.")
            log_meta_capability_failure(provider="instagram_login", probe="profile_identity", error=error)
            raise error
        try:
            profile = client.instagram_profile(provider_account_id, instagram_token)
            external_id, _, _, _ = profile_identity(profile)
        except InstagramMetaError as error:
            log_meta_capability_failure(
                provider="instagram_login", probe="profile_identity", error=error,
            )
            raise
        if external_id != provider_account_id:
            error = InstagramMetaError("identity_mismatch", "A credencial não corresponde à conta vinculada ao projeto.")
            log_meta_capability_failure(provider="instagram_login", probe="profile_identity", error=error)
            raise error
        try:
            media = client.sample_media(external_id, instagram_token)
        except InstagramMetaError as error:
            log_meta_capability_failure(provider="instagram_login", probe="media_sample", error=error)
            raise
        comments = _sample_comments_capability(media, client, instagram_token, provider="instagram_login")
        media_types = sorted({_media_type(row) for row in media if str(row.get("id") or "").strip()})
        has_insights_scope = INSTAGRAM_LOGIN_INSIGHTS_SCOPE in set(connection.scopes_json or [])
        if has_insights_scope:
            samples_by_type = _media_samples_by_type(media)
            metrics = {
                metric: _probe_media_metric(
                    client, samples_by_type, metric, instagram_token, provider="instagram_login"
                )
                for metric in CAPABILITY_METRICS
            }
            try:
                account_payload = client.account_insights(external_id, instagram_token)
                account_metrics = {"status": "supported", **_temporal_metadata(account_payload)}
            except InstagramMetaError as error:
                log_meta_capability_failure(
                    provider="instagram_login", probe="account_insights", metric="reach", error=error,
                )
                account_metrics = {"status": _metric_error_status(error), "temporal_semantics": "unknown"}
        else:
            # Conexões criadas antes do novo consentimento seguem válidas para
            # perfil, mídia e comentários; a reautorização concede Insights.
            metrics = {
                metric: {
                    "status": "permission_denied",
                    "by_media_type": {media_type: "permission_denied" for media_type in media_types},
                    "temporal_by_media_type": {},
                }
                for metric in CAPABILITY_METRICS
            }
            account_metrics = {"status": "permission_denied", "temporal_semantics": "unknown"}
        result = {
            "checked_at": now.isoformat(),
            "api_version": settings.api_version,
            "provider": "instagram_login",
            "profile": {"status": "supported", "fields": _profile_field_status(profile)},
            "sample": {
                "status": "supported",
                "limit": CAPABILITY_MEDIA_LIMIT,
                "observed_count": len(media),
                "media_types": media_types,
            },
            "comments": comments,
            "media_metrics": metrics,
            "account_metrics": account_metrics,
            "duration_ms": round((monotonic() - started) * 1000),
        }
    except InstagramMetaError as error:
        connection.status = "needs_reauth" if error.category in {"reconnect_required", "auth_expired"} else "error"
        connection.instagram_account.connection_status = connection.status
        connection.last_error_category = error.category
        connection.last_error_at = now
        db.session.flush()
        raise
    connection.status = "connected"
    connection.instagram_account.connection_status = "connected"
    connection.api_version = settings.api_version
    connection.capability_result_json = result
    connection.last_capability_test_at = now
    connection.last_error_category = None
    connection.last_error_at = None
    db.session.flush()
    return result


def run_capability_test(
    connection: InstagramApiConnection,
    settings: InstagramMetaSettings,
    client: Any,
) -> dict[str, Any]:
    """Executa somente o diagnóstico correspondente ao provider ativo."""
    runners = {
        "facebook_login": _run_facebook_capability_test,
        "instagram_login": _run_instagram_capability_test,
    }
    try:
        runner = runners[connection.provider]
    except KeyError:
        error = InstagramMetaError("configuration", "O método de conexão armazenado não é suportado.")
        log_meta_capability_failure(provider=connection.provider, probe="capability_setup", error=error)
        raise error from None
    try:
        return runner(connection, settings, client)
    except InstagramMetaError as error:
        if not getattr(error, "_capability_log_emitted", False):
            log_meta_capability_failure(
                provider=connection.provider,
                probe="capability_setup",
                error=error,
            )
        raise


def disconnect_connection(connection: InstagramApiConnection) -> None:
    """Revoga localmente a conexão, sem remover conta, histórico ou capacidade registrada."""
    connection.status = "revoked"
    connection.encrypted_access_token = None
    connection.encrypted_refresh_token = None
    connection.token_expires_at = None
    connection.last_error_category = None
    connection.last_error_at = None
    connection.instagram_account.connection_status = "not_connected"
    db.session.flush()


def log_meta_operation(project_id: str, operation: str, status: str, *, http_status: int | None = None, duration_ms: int | None = None) -> None:
    """Loga somente metadados operacionais seguros, sem dados secretos ou payloads."""
    logging.getLogger(__name__).info(
        "instagram_meta project=%s operation=%s status=%s http_status=%s duration_ms=%s",
        project_id, operation, status, http_status, duration_ms,
    )
