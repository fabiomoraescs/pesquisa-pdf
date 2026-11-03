"""Cliente defensivo e independente para o protocolo OAI-PMH de periódicos OJS.

O módulo não conhece Flask, SQLAlchemy, modelos ou armazenamento da plataforma.
Ele é deliberadamente estrito ao aceitar endpoints: a coleta pública só pode usar
HTTPS na porta 443, em hosts DNS que resolvam exclusivamente para endereços
globais. A conexão é aberta para o IP validado, conservando o hostname para SNI
e validação normal do certificado TLS; portanto, não há uma segunda resolução
DNS entre a validação e a conexão.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import http.client
import ipaddress
import re
import socket
import ssl
import time
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlencode, urlsplit

from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException


OAI_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
DC_FIELDS = frozenset(
    {"title", "creator", "subject", "description", "identifier", "date", "language", "type"}
)
DEFAULT_USER_AGENT = "Analisys-OJS-SEO/1.0"


class OaiClientError(RuntimeError):
    """Erro seguro para apresentação ou registro de uma coleta OAI-PMH."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class EndpointValidationError(OaiClientError):
    """Endpoint recusado antes de qualquer chamada HTTP."""


class OaiTransportError(OaiClientError):
    """Falha segura de transporte HTTP/TLS.

    ``retry_after_seconds`` é somente uma instrução de transporte efêmera. Ela
    não é persistida nem apresentada à interface, pois cabe ao cliente decidir
    se ainda há orçamento de execução para respeitá-la.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code, message)
        self.retry_after_seconds = retry_after_seconds


class OaiProtocolError(OaiClientError):
    """Resposta HTTP recebida, mas inválida para OAI-PMH."""


@dataclass(frozen=True, slots=True)
class OaiClientConfig:
    """Limites conservadores para uma auditoria pública de OAI-PMH."""

    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    max_execution_seconds: float = 120.0
    max_pages: int = 100
    max_records: int = 5_000
    max_response_bytes: int = 2 * 1024 * 1024
    max_total_response_bytes: int = 20 * 1024 * 1024
    min_request_interval_seconds: float = 0.5
    max_resumption_token_length: int = 4_096
    max_request_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    max_retry_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        positive_numbers = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.max_execution_seconds,
            self.max_pages,
            self.max_records,
            self.max_response_bytes,
            self.max_total_response_bytes,
            self.max_resumption_token_length,
            self.max_request_attempts,
            self.retry_backoff_seconds,
            self.max_retry_delay_seconds,
        )
        if any(value <= 0 for value in positive_numbers):
            raise ValueError("Os limites de coleta devem ser positivos.")
        if self.min_request_interval_seconds < 0:
            raise ValueError("O intervalo mínimo entre requisições não pode ser negativo.")
        if not isinstance(self.max_request_attempts, int) or isinstance(self.max_request_attempts, bool):
            raise ValueError("O número de tentativas por requisição deve ser inteiro.")
        if self.max_total_response_bytes < self.max_response_bytes:
            raise ValueError("O limite acumulado não pode ser menor que o limite por resposta.")


@dataclass(frozen=True, slots=True)
class ValidatedOaiEndpoint:
    """Endpoint validado uma única vez e endereços DNS públicos fixados."""

    canonical_url: str
    host: str
    port: int
    path: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Resposta já limitada, entregue pelo transporte ao cliente de protocolo."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class OaiIssue:
    """Problema sanitizado: não contém XML, tokens, caminhos ou dados privados."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class OaiRepositoryIdentity:
    repository_name: str
    base_url: str
    protocol_version: str
    earliest_datestamp: str | None = None
    deleted_record_policy: str | None = None
    granularity: str | None = None


@dataclass(frozen=True, slots=True)
class OaiMetadataFormat:
    metadata_prefix: str
    schema: str | None
    metadata_namespace: str | None


@dataclass(frozen=True, slots=True)
class OaiRecord:
    """Registro OAI-PMH ativo, sem inferir campos ausentes do HTML do periódico."""

    identifier: str
    datestamp: str | None
    set_specs: tuple[str, ...]
    metadata_present: bool
    fields: Mapping[str, tuple[str, ...]]
    dois: tuple[str, ...]

    @property
    def has_normalized_metadata(self) -> bool:
        return self.metadata_present and any(self.fields.values())


@dataclass(frozen=True, slots=True)
class OaiDeletedRecord:
    identifier: str
    datestamp: str | None
    set_specs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OaiCollectionResult:
    """Observações da coleta e métricas derivadas, mantidas separadamente."""

    status: str
    completion_reason: str | None
    pages_fetched: int
    total_response_bytes: int
    records: tuple[OaiRecord, ...]
    deleted_records: tuple[OaiDeletedRecord, ...]
    unique_identifiers: tuple[str, ...]
    duplicate_identifiers: tuple[str, ...]
    warnings: tuple[OaiIssue, ...] = ()
    errors: tuple[OaiIssue, ...] = ()
    elapsed_seconds: float = 0.0

    @property
    def metrics(self) -> Mapping[str, int]:
        """Métricas derivadas; não reescrevem as observações da coleta."""
        normalized_records = tuple(record for record in self.records if record.has_normalized_metadata)
        return {
            "active_records": len(self.records),
            "deleted_records": len(self.deleted_records),
            "records_with_normalized_metadata": len(normalized_records),
        "records_with_doi": sum(1 for record in self.records if record.dois),
        "identified_dois": len({doi for record in self.records for doi in record.dois}),
            "unique_identifiers": len(self.unique_identifiers),
            "duplicate_identifiers": len(self.duplicate_identifiers),
            "pages_fetched": self.pages_fetched,
            "total_response_bytes": self.total_response_bytes,
        }

    def field_coverage(self) -> Mapping[str, int]:
        """Quantidade de registros ativos com ao menos um valor por campo DC."""
        return {
            field: sum(1 for record in self.records if record.fields.get(field))
            for field in sorted(DC_FIELDS)
        }


@dataclass(frozen=True, slots=True)
class OaiPmhAuditResult:
    endpoint: str
    status: str
    identity: OaiRepositoryIdentity | None
    metadata_formats: tuple[OaiMetadataFormat, ...]
    collection: OaiCollectionResult | None
    errors: tuple[OaiIssue, ...] = ()
    warnings: tuple[OaiIssue, ...] = ()


class OaiHttpTransport(Protocol):
    def get(
        self,
        endpoint: ValidatedOaiEndpoint,
        params: Mapping[str, str],
        config: OaiClientConfig,
    ) -> HttpResponse:
        """Executa uma requisição segura e retorna somente o corpo já limitado."""


Resolver = Callable[..., Sequence[tuple]]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def _is_public_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _dns_addresses(host: str, resolver: Resolver) -> tuple[str, ...]:
    try:
        resolved = resolver(host, 443, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise EndpointValidationError(
            "dns_resolution_failed",
            "Não foi possível validar publicamente o host informado.",
        ) from exc

    addresses: list[str] = []
    for entry in resolved:
        try:
            address = str(entry[4][0])
        except (IndexError, TypeError):
            raise EndpointValidationError(
                "invalid_dns_answer",
                "A resolução do host não forneceu endereços utilizáveis.",
            ) from None
        if not _is_public_address(address):
            raise EndpointValidationError(
                "non_public_dns_address",
                "O host informado não pode apontar para endereços locais, privados ou reservados.",
            )
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise EndpointValidationError(
            "empty_dns_answer", "A resolução do host não forneceu endereços públicos utilizáveis."
        )
    return tuple(addresses)


def validate_oai_endpoint(endpoint_url: str, *, resolver: Resolver = socket.getaddrinfo) -> ValidatedOaiEndpoint:
    """Valida URL pública de OAI-PMH sem abrir HTTP ou seguir redirecionamentos."""
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise EndpointValidationError("invalid_endpoint", "Informe um endpoint OAI-PMH HTTPS válido.")
    if len(endpoint_url) > 2_048 or any(ord(char) < 32 for char in endpoint_url):
        raise EndpointValidationError("invalid_endpoint", "O endpoint informado contém caracteres não permitidos.")

    try:
        parsed = urlsplit(endpoint_url.strip())
    except ValueError as exc:
        raise EndpointValidationError("invalid_endpoint", "O endpoint informado não é uma URL válida.") from exc
    if parsed.scheme.lower() != "https":
        raise EndpointValidationError("https_required", "O endpoint OAI-PMH deve utilizar HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointValidationError(
            "embedded_credentials", "O endpoint OAI-PMH não pode conter credenciais na URL."
        )
    if parsed.query or parsed.fragment:
        raise EndpointValidationError(
            "endpoint_query_not_allowed", "O endpoint deve ser informado sem parâmetros ou fragmentos."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise EndpointValidationError("invalid_endpoint_port", "A porta do endpoint é inválida.") from exc
    if port not in (None, 443):
        raise EndpointValidationError("https_port_required", "O endpoint OAI-PMH deve utilizar a porta HTTPS 443.")
    if not parsed.hostname:
        raise EndpointValidationError("missing_host", "O endpoint informado não possui host.")

    raw_host = parsed.hostname.rstrip(".")
    try:
        ipaddress.ip_address(raw_host)
    except ValueError:
        pass
    else:
        raise EndpointValidationError(
            "ip_literal_not_allowed", "Informe um nome DNS público, não um endereço IP literal."
        )
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise EndpointValidationError("invalid_host", "O host do endpoint é inválido.") from exc

    path = parsed.path or "/"
    if not path.startswith("/") or "/../" in f"{path}/":
        raise EndpointValidationError("invalid_endpoint_path", "O caminho do endpoint é inválido.")
    addresses = _dns_addresses(host, resolver)
    canonical_url = f"https://{host}{path}"
    return ValidatedOaiEndpoint(canonical_url, host, 443, path, addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Conexão TLS que disca o IP já validado, mantendo SNI no hostname DNS."""

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host=host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        raw_socket.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


class PinnedHttpsTransport:
    """Transporte sem proxies do ambiente, sem redirects e com limites de leitura."""

    def __init__(
        self,
        *,
        connection_factory: Callable[[str, str, int, float, ssl.SSLContext], http.client.HTTPSConnection]
        | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._connection_factory = connection_factory or _PinnedHTTPSConnection
        self._ssl_context = ssl_context or ssl.create_default_context()

    def get(
        self,
        endpoint: ValidatedOaiEndpoint,
        params: Mapping[str, str],
        config: OaiClientConfig,
    ) -> HttpResponse:
        encoded = urlencode(params, doseq=False, safe="")
        target = endpoint.path + (f"?{encoded}" if encoded else "")
        last_error: OaiTransportError | None = None
        request_deadline = time.monotonic() + config.max_execution_seconds
        for address in endpoint.addresses:
            remaining_budget = request_deadline - time.monotonic()
            if remaining_budget <= 0:
                raise OaiTransportError("execution_time_limit", "A coleta atingiu o tempo máximo permitido.")
            connection = self._connection_factory(
                endpoint.host,
                address,
                endpoint.port,
                min(config.connect_timeout_seconds, remaining_budget),
                self._ssl_context,
            )
            phase = "connect"
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Host": endpoint.host,
                        "Accept": "application/xml, text/xml;q=0.9",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                        "User-Agent": DEFAULT_USER_AGENT,
                    },
                )
                active_socket = getattr(connection, "sock", None)
                if active_socket is not None:
                    remaining_budget = request_deadline - time.monotonic()
                    if remaining_budget <= 0:
                        raise OaiTransportError("execution_time_limit", "A coleta atingiu o tempo máximo permitido.")
                    active_socket.settimeout(min(config.read_timeout_seconds, remaining_budget))
                phase = "read"
                response = connection.getresponse()
                headers = {name.lower(): value for name, value in response.getheaders()}
                if 300 <= response.status < 400:
                    raise OaiTransportError(
                        "redirect_not_allowed", "O endpoint solicitou redirecionamento, que não é aceito por segurança."
                    )
                if not 200 <= response.status < 300:
                    raise _http_error(response.status, headers)
                encoding = headers.get("content-encoding", "identity").lower().strip()
                if encoding not in ("", "identity"):
                    raise OaiTransportError(
                        "compressed_response_not_allowed",
                        "O endpoint retornou uma codificação de conteúdo não aceita para a coleta segura.",
                    )
                _enforce_content_length(headers.get("content-length"), config.max_response_bytes)
                body = _read_limited_body(response, config.max_response_bytes, deadline=request_deadline)
                return HttpResponse(response.status, headers, body)
            except OaiClientError:
                # Respostas HTTP, redirects, codificação e limites são decisões
                # definitivas do servidor. Não tentamos outro IP para evitar
                # multiplicar uma consulta recusada ou limitada.
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
                last_error = _transport_error_from_exception(exc, phase=phase)
                # Falhas de TLS nunca são retomadas em outro endereço DNS nem
                # pelo mecanismo de repetição do cliente: a validação do
                # certificado é uma fronteira de segurança, não uma oscilação
                # transitória do protocolo.
                if last_error.code.startswith("tls_"):
                    raise last_error from exc
            finally:
                connection.close()
        assert last_error is not None
        raise last_error


def _enforce_content_length(value: str | None, maximum: int) -> None:
    if value is None:
        return
    try:
        size = int(value)
    except ValueError as exc:
        raise OaiTransportError("invalid_content_length", "O endpoint retornou um tamanho de resposta inválido.") from exc
    if size < 0 or size > maximum:
        raise OaiTransportError("response_too_large", "A resposta do endpoint excede o limite permitido.")


def _read_limited_body(
    response: http.client.HTTPResponse,
    maximum: int,
    *,
    deadline: float | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise OaiTransportError("execution_time_limit", "A coleta atingiu o tempo máximo permitido.")
            chunk = response.read(min(64 * 1024, maximum + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise OaiTransportError("response_too_large", "A resposta do endpoint excede o limite permitido.")
            chunks.append(chunk)
    except OaiClientError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise _transport_error_from_exception(exc, phase="read") from exc
    return b"".join(chunks)


def _transport_error_from_exception(exc: BaseException, *, phase: str) -> OaiTransportError:
    """Classifica falhas de socket sem revelar detalhes da infraestrutura.

    A classificação é usada exclusivamente para decidir uma repetição limitada
    e para diagnóstico sanitizado. Não conserva IP, host, token de paginação ou
    texto da exceção original.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return OaiTransportError(
            "tls_certificate_failed", "Não foi possível validar o certificado TLS do endpoint OAI-PMH."
        )
    if isinstance(exc, ssl.SSLError):
        return OaiTransportError("tls_failed", "Não foi possível estabelecer uma conexão TLS segura com o endpoint OAI-PMH.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        if phase == "connect":
            return OaiTransportError("connect_timeout", "A conexão com o endpoint OAI-PMH excedeu o prazo permitido.")
        return OaiTransportError("read_timeout", "A resposta do endpoint OAI-PMH excedeu o prazo permitido.")
    if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        return OaiTransportError("connection_interrupted", "A conexão com o endpoint OAI-PMH foi interrompida.")
    if phase == "read":
        return OaiTransportError("response_interrupted", "A resposta do endpoint foi interrompida antes da conclusão.")
    return OaiTransportError("transport_failed", "Não foi possível obter uma resposta segura do endpoint OAI-PMH.")


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Converte Retry-After sem usar dados do endpoint em diagnósticos."""
    value = headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if retry_at.tzinfo is None:
        return None
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _http_error(status: int, headers: Mapping[str, str] | None = None) -> OaiTransportError:
    messages = {
        401: "O endpoint exige autenticação e não pode ser consultado publicamente.",
        403: "O endpoint recusou a consulta pública solicitada.",
        404: "O endpoint OAI-PMH não foi encontrado.",
        429: "O endpoint limitou temporariamente as requisições. Tente novamente mais tarde.",
    }
    return OaiTransportError(
        f"http_{status}",
        messages.get(status, "O endpoint retornou uma falha HTTP."),
        retry_after_seconds=_retry_after_seconds(headers or {}) if status in {429, 503} else None,
    )


_RETRYABLE_TRANSPORT_CODES = frozenset(
    {
        "connect_timeout",
        "read_timeout",
        "connection_interrupted",
        "response_interrupted",
        "transport_failed",
        # Estes são os únicos status HTTP tratados como transitórios. Os
        # demais 4xx, redirects, erros OAI-PMH e respostas fora dos limites
        # continuam definitivos e não recebem nova chamada automática.
        "http_408",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
    }
)


def _is_retryable_transport_error(error: OaiClientError) -> bool:
    return isinstance(error, OaiTransportError) and error.code in _RETRYABLE_TRANSPORT_CODES


def _local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _namespace(tag: object) -> str | None:
    if not isinstance(tag, str) or not tag.startswith("{"):
        return None
    return tag[1:].partition("}")[0]


def _child(element, local_name: str):
    return next((child for child in element if _local_name(child.tag) == local_name), None)


def _children(element, local_name: str):
    return tuple(child for child in element if _local_name(child.tag) == local_name)


def _text(element) -> str | None:
    if element is None or element.text is None:
        return None
    value = " ".join(element.text.split())
    return value or None


def _parse_xml(body: bytes):
    try:
        root = DefusedElementTree.fromstring(body)
    except (DefusedXmlException, DefusedElementTree.ParseError, ValueError, UnicodeError) as exc:
        raise OaiProtocolError(
            "unsafe_or_invalid_xml", "O endpoint retornou XML inválido ou não seguro para processamento."
        ) from exc
    if root.tag != f"{{{OAI_NAMESPACE}}}OAI-PMH":
        raise OaiProtocolError("invalid_oai_envelope", "A resposta não contém um envelope OAI-PMH válido.")
    errors = _children(root, "error")
    if errors:
        code = (errors[0].attrib.get("code") or "unknown").strip()
        normalized = "".join(char for char in code if char.isalnum() or char in "_-.")[:80] or "unknown"
        raise OaiProtocolError(
            f"oai_error_{normalized}", f"O endpoint OAI-PMH informou o erro de protocolo: {normalized}."
        )
    return root


def _parse_dois(identifiers: Sequence[str]) -> tuple[str, ...]:
    """Extrai somente DOI explícito; URLs de artigos nunca são tratadas como DOI."""
    dois: list[str] = []
    for identifier in identifiers:
        candidate = identifier.strip()
        lowered = candidate.lower()
        if lowered.startswith("https://doi.org/") or lowered.startswith("http://doi.org/"):
            candidate = candidate.split("doi.org/", 1)[1]
        elif lowered.startswith("doi:"):
            candidate = candidate[4:].strip()
        if _is_valid_doi(candidate) and candidate not in dois:
            dois.append(candidate)
    return tuple(dois)


def _is_valid_doi(value: str) -> bool:
    return len(value) <= 500 and bool(
        re.fullmatch(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", value, flags=re.IGNORECASE)
    )


class OaiPmhClient:
    """Cliente OAI-PMH seguro e testável, projetado para futura integração OJS SEO."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        config: OaiClientConfig | None = None,
        resolver: Resolver = socket.getaddrinfo,
        transport: OaiHttpTransport | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self.config = config or OaiClientConfig()
        self.endpoint = validate_oai_endpoint(endpoint_url, resolver=resolver)
        self._transport = transport or PinnedHttpsTransport()
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None

    def identify(self, *, _deadline: float | None = None) -> OaiRepositoryIdentity:
        root = self._request_xml({"verb": "Identify"}, deadline=_deadline)
        identify = _child(root, "Identify")
        if identify is None:
            raise OaiProtocolError("missing_identify", "A resposta OAI-PMH não contém a identificação do repositório.")
        identity = OaiRepositoryIdentity(
            repository_name=_text(_child(identify, "repositoryName")) or "",
            base_url=_text(_child(identify, "baseURL")) or "",
            protocol_version=_text(_child(identify, "protocolVersion")) or "",
            earliest_datestamp=_text(_child(identify, "earliestDatestamp")),
            deleted_record_policy=_text(_child(identify, "deletedRecord")),
            granularity=_text(_child(identify, "granularity")),
        )
        if not identity.repository_name or not identity.base_url or identity.protocol_version != "2.0":
            raise OaiProtocolError(
                "invalid_identify", "O endpoint não apresentou uma identificação OAI-PMH 2.0 completa."
            )
        return identity

    def list_metadata_formats(self, *, _deadline: float | None = None) -> tuple[OaiMetadataFormat, ...]:
        root = self._request_xml({"verb": "ListMetadataFormats"}, deadline=_deadline)
        result = _child(root, "ListMetadataFormats")
        if result is None:
            raise OaiProtocolError(
                "missing_metadata_formats", "A resposta OAI-PMH não contém formatos de metadados."
            )
        formats: list[OaiMetadataFormat] = []
        for item in _children(result, "metadataFormat"):
            prefix = _text(_child(item, "metadataPrefix"))
            if not prefix:
                continue
            formats.append(
                OaiMetadataFormat(
                    prefix,
                    _text(_child(item, "schema")),
                    _text(_child(item, "metadataNamespace")),
                )
            )
        return tuple(formats)

    def collect_oai_dc(self, *, _deadline: float | None = None) -> OaiCollectionResult:
        started_at = self._clock()
        deadline = _deadline if _deadline is not None else started_at + self.config.max_execution_seconds
        pages = 0
        total_bytes = 0
        active: list[OaiRecord] = []
        deleted: list[OaiDeletedRecord] = []
        unique_identifiers: list[str] = []
        duplicates: list[str] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        seen_pages: set[str] = set()
        warnings: list[OaiIssue] = []
        token: str | None = None

        while True:
            if self._clock() >= deadline:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "execution_time_limit", "A coleta atingiu o tempo máximo permitido.", self._elapsed(started_at)
                )
            params = {"verb": "ListRecords", "resumptionToken": token} if token else {
                "verb": "ListRecords", "metadataPrefix": "oai_dc"
            }
            try:
                root, response_size = self._request_xml_with_size(params, deadline=deadline)
            except OaiClientError as exc:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    exc.code, exc.safe_message, self._elapsed(started_at)
                )
            pages += 1
            total_bytes += response_size
            if total_bytes > self.config.max_total_response_bytes:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "total_response_size_limit", "A coleta atingiu o limite total de dados permitido.", self._elapsed(started_at)
                )
            page_digest = sha256(self._last_response_body).hexdigest()
            if page_digest in seen_pages:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "repeated_page", "O endpoint repetiu uma página de resultados.", self._elapsed(started_at)
                )
            seen_pages.add(page_digest)
            records_container = _child(root, "ListRecords")
            if records_container is None:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "missing_list_records", "A resposta OAI-PMH não contém registros para o formato solicitado.", self._elapsed(started_at)
                )

            record_limit_reached = False
            for element in _children(records_container, "record"):
                parsed = _parse_record(element)
                if parsed is None:
                    warnings.append(OaiIssue("invalid_record", "Um registro OAI-PMH sem identificador foi ignorado."))
                    continue
                identifier = parsed.identifier
                if identifier in seen_ids:
                    if identifier not in duplicates:
                        duplicates.append(identifier)
                    continue
                if len(unique_identifiers) >= self.config.max_records:
                    record_limit_reached = True
                    break
                seen_ids.add(identifier)
                unique_identifiers.append(identifier)
                if isinstance(parsed, OaiDeletedRecord):
                    deleted.append(parsed)
                else:
                    active.append(parsed)

            next_token = _text(_child(records_container, "resumptionToken"))
            if record_limit_reached:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "record_limit_reached", "A coleta atingiu o limite de registros permitido.", self._elapsed(started_at)
                )
            if not next_token:
                return OaiCollectionResult(
                    "completed", None, pages, total_bytes, tuple(active), tuple(deleted),
                    tuple(unique_identifiers), tuple(duplicates), tuple(warnings), (), self._elapsed(started_at)
                )
            if len(next_token) > self.config.max_resumption_token_length:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "resumption_token_too_large", "O endpoint retornou um token de paginação acima do limite seguro.", self._elapsed(started_at)
                )
            if next_token in seen_tokens:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "repeated_resumption_token", "O endpoint repetiu um token de paginação.", self._elapsed(started_at)
                )
            if pages >= self.config.max_pages:
                return self._partial_or_failed(
                    pages, total_bytes, active, deleted, unique_identifiers, duplicates, warnings,
                    "page_limit_reached", "A coleta atingiu o limite de páginas permitido.", self._elapsed(started_at)
                )
            seen_tokens.add(next_token)
            token = next_token

    def inspect(self) -> OaiPmhAuditResult:
        """Executa descoberta e coleta; falhas são retornadas sem dados de transporte sensíveis."""
        deadline = self._clock() + self.config.max_execution_seconds
        try:
            identity = self.identify(_deadline=deadline)
            formats = self.list_metadata_formats(_deadline=deadline)
        except OaiClientError as exc:
            return OaiPmhAuditResult(
                self.endpoint.canonical_url,
                "failed",
                None,
                (),
                None,
                (OaiIssue(exc.code, exc.safe_message),),
            )
        if "oai_dc" not in {item.metadata_prefix for item in formats}:
            issue = OaiIssue("oai_dc_not_advertised", "O endpoint não anunciou o formato Dublin Core oai_dc.")
            return OaiPmhAuditResult(
                self.endpoint.canonical_url, "failed", identity, formats, None, (issue,)
            )
        collection = self.collect_oai_dc(_deadline=deadline)
        return OaiPmhAuditResult(
            self.endpoint.canonical_url,
            collection.status,
            identity,
            formats,
            collection,
            collection.errors,
            collection.warnings,
        )

    def _request_xml(self, params: Mapping[str, str], *, deadline: float | None = None):
        root, _ = self._request_xml_with_size(params, deadline=deadline)
        return root

    def _request_xml_with_size(self, params: Mapping[str, str], *, deadline: float | None = None):
        """Busca e analisa uma página, repetindo somente falhas transitórias.

        ``params`` é preservado imutável durante as tentativas. Assim, uma
        falha numa página de ``resumptionToken`` usa exatamente o mesmo token
        até obter uma resposta válida ou consumir as três tentativas.
        """
        for attempt in range(1, self.config.max_request_attempts + 1):
            self._wait_for_rate_limit(deadline=deadline)
            request_config = self._config_with_remaining_budget(deadline)
            try:
                try:
                    response = self._transport.get(self.endpoint, params, request_config)
                except OaiClientError:
                    raise
                except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
                    raise _transport_error_from_exception(exc, phase="connect") from exc
                finally:
                    # Mesmo uma conexão que falha consumiu uma tentativa. A
                    # próxima chamada respeita o intervalo mínimo configurado.
                    self._last_request_at = self._clock()

                self._validate_response(response)
                self._ensure_within_deadline(deadline)
                self._last_response_body = response.body
                return _parse_xml(response.body), len(response.body)
            except OaiClientError as exc:
                delay = self._retry_delay(exc, attempt=attempt, deadline=deadline)
                if delay is None:
                    raise
                self._sleep_for_retry(delay, deadline=deadline)

        raise AssertionError("A política de tentativas deveria sempre retornar ou propagar um erro.")

    def _validate_response(self, response: HttpResponse) -> None:
        if 300 <= response.status < 400:
            raise OaiTransportError(
                "redirect_not_allowed", "O endpoint solicitou redirecionamento, que não é aceito por segurança."
            )
        if not 200 <= response.status < 300:
            raise _http_error(response.status, response.headers)
        if len(response.body) > self.config.max_response_bytes:
            raise OaiTransportError("response_too_large", "A resposta do endpoint excede o limite permitido.")
        if response.headers.get("content-encoding", "identity").lower().strip() not in ("", "identity"):
            raise OaiTransportError(
                "compressed_response_not_allowed",
                "O endpoint retornou uma codificação de conteúdo não aceita para a coleta segura.",
            )
        _enforce_content_length(response.headers.get("content-length"), self.config.max_response_bytes)

    def _wait_for_rate_limit(self, *, deadline: float | None = None) -> None:
        if self._last_request_at is None or not self.config.min_request_interval_seconds:
            return
        remaining = self.config.min_request_interval_seconds - (self._clock() - self._last_request_at)
        if remaining > 0:
            self._ensure_delay_fits_budget(remaining, deadline=deadline)
            self._sleeper(remaining)
            self._ensure_within_deadline(deadline)

    def _config_with_remaining_budget(self, deadline: float | None) -> OaiClientConfig:
        if deadline is None:
            return self.config
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise OaiTransportError("execution_time_limit", "A coleta atingiu o tempo máximo permitido.")
        # Impede que uma tentativa iniciada no fim do orçamento configure um
        # timeout de socket maior que o tempo de posse restante.
        return replace(
            self.config,
            connect_timeout_seconds=min(self.config.connect_timeout_seconds, remaining),
            read_timeout_seconds=min(self.config.read_timeout_seconds, remaining),
            max_execution_seconds=min(self.config.max_execution_seconds, remaining),
        )

    def _retry_delay(self, error: OaiClientError, *, attempt: int, deadline: float | None) -> float | None:
        if not _is_retryable_transport_error(error) or attempt >= self.config.max_request_attempts:
            return None
        requested = error.retry_after_seconds if isinstance(error, OaiTransportError) else None
        if requested is not None and requested > self.config.max_retry_delay_seconds:
            # Não repetimos antes do prazo indicado pelo servidor, nem
            # suspendemos um worker por um intervalo excessivo.
            return None
        delay = max(
            self.config.retry_backoff_seconds * (2 ** (attempt - 1)),
            requested or 0.0,
        )
        self._ensure_delay_fits_budget(delay, deadline=deadline)
        return delay

    def _sleep_for_retry(self, delay: float, *, deadline: float | None) -> None:
        self._sleeper(delay)
        self._ensure_within_deadline(deadline)

    def _ensure_delay_fits_budget(self, delay: float, *, deadline: float | None) -> None:
        if deadline is not None and self._clock() + delay >= deadline:
            raise OaiTransportError("execution_time_limit", "A coleta atingiu o tempo máximo permitido.")

    def _ensure_within_deadline(self, deadline: float | None) -> None:
        if deadline is not None and self._clock() >= deadline:
            raise OaiTransportError("execution_time_limit", "A coleta atingiu o tempo máximo permitido.")

    def _elapsed(self, started_at: float) -> float:
        return max(0.0, self._clock() - started_at)

    @staticmethod
    def _partial_or_failed(
        pages: int,
        total_bytes: int,
        active: list[OaiRecord],
        deleted: list[OaiDeletedRecord],
        unique_identifiers: list[str],
        duplicates: list[str],
        warnings: list[OaiIssue],
        code: str,
        message: str,
        elapsed_seconds: float,
    ) -> OaiCollectionResult:
        issue = OaiIssue(code, message)
        status = "partial" if pages else "failed"
        return OaiCollectionResult(
            status,
            code,
            pages,
            total_bytes,
            tuple(active),
            tuple(deleted),
            tuple(unique_identifiers),
            tuple(duplicates),
            tuple(warnings),
            (issue,),
            elapsed_seconds,
        )


def _parse_record(element) -> OaiRecord | OaiDeletedRecord | None:
    header = _child(element, "header")
    if header is None:
        return None
    identifier = _text(_child(header, "identifier"))
    if not identifier:
        return None
    datestamp = _text(_child(header, "datestamp"))
    set_specs = tuple(value for value in (_text(item) for item in _children(header, "setSpec")) if value)
    if (header.attrib.get("status") or "").lower() == "deleted":
        return OaiDeletedRecord(identifier, datestamp, set_specs)

    metadata = _child(element, "metadata")
    values: dict[str, list[str]] = {field: [] for field in DC_FIELDS}
    if metadata is not None:
        for child in metadata.iter():
            name = _local_name(child.tag)
            value = _text(child)
            if (
                name in DC_FIELDS
                and _namespace(child.tag) == DC_NAMESPACE
                and value
                and value not in values[name]
            ):
                values[name].append(value)
    fields = {field: tuple(values[field]) for field in sorted(DC_FIELDS)}
    return OaiRecord(
        identifier,
        datestamp,
        set_specs,
        metadata is not None,
        fields,
        _parse_dois(fields["identifier"]),
    )


__all__ = [
    "DC_FIELDS",
    "EndpointValidationError",
    "HttpResponse",
    "OaiClientConfig",
    "OaiClientError",
    "OaiCollectionResult",
    "OaiDeletedRecord",
    "OaiIssue",
    "OaiMetadataFormat",
    "OaiPmhAuditResult",
    "OaiPmhClient",
    "OaiProtocolError",
    "OaiRecord",
    "OaiRepositoryIdentity",
    "OaiTransportError",
    "PinnedHttpsTransport",
    "ValidatedOaiEndpoint",
    "validate_oai_endpoint",
]
