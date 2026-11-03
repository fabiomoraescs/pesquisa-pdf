"""Cliente defensivo e analisador determinístico de páginas HTML públicas.

Este módulo reaproveita os primitivos de conexão fixada por DNS, TLS e limites
de leitura do cliente OAI-PMH, mas não altera o protocolo OAI nem aceita URLs
de usuários sem validação. Nenhum HTML bruto é retornado para persistência.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit
import unicodedata

from .ojs_oai_client import (
    DEFAULT_USER_AGENT,
    EndpointValidationError,
    OaiClientError,
    OaiTransportError,
    _PinnedHTTPSConnection,
    _enforce_content_length,
    _http_error,
    _is_retryable_transport_error,
    _read_limited_body,
    _transport_error_from_exception,
)


Resolver = Callable[..., Sequence[tuple]]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


_HTML_TRANSPORT_MESSAGES = {
    "connect_timeout": "A conexão com a página HTML excedeu o prazo permitido.",
    "read_timeout": "A resposta da página HTML excedeu o prazo permitido.",
    "connection_interrupted": "A conexão com a página HTML foi interrompida.",
    "response_interrupted": "A resposta da página HTML foi interrompida.",
    "transport_failed": "Não foi possível obter uma resposta segura da página HTML.",
    "tls_certificate_failed": "Não foi possível validar o certificado TLS da página HTML.",
    "tls_failed": "Não foi possível estabelecer uma conexão TLS segura com a página HTML.",
    "execution_time_limit": "A análise HTML atingiu o tempo máximo permitido.",
    "invalid_content_length": "A página HTML retornou um tamanho de resposta inválido.",
    "response_too_large": "A resposta da página HTML excede o limite permitido.",
}


# Estes identificadores permanecem no histórico e nos testes. A camada de
# interface recebe apenas o rótulo compreensível, sem expor a chave técnica.
_HTML_FINDING_LABELS = {
    "html_unavailable": "Página HTML não verificada",
    "invalid_html_url": "URL pública inválida",
    "https_required": "HTTPS obrigatório",
    "embedded_credentials": "URL com credenciais recusada",
    "invalid_html_port": "Porta da URL inválida",
    "https_port_required": "Porta HTTPS obrigatória",
    "ip_literal_not_allowed": "Endereço IP direto recusado",
    "invalid_host": "Host da URL inválido",
    "invalid_html_path": "Caminho da URL inválido",
    "dns_resolution_failed": "Host público não pôde ser validado",
    "non_public_dns_address": "Destino não público recusado",
    "empty_dns_answer": "Host sem endereço público utilizável",
    "connect_timeout": "Tempo de conexão excedido",
    "read_timeout": "Tempo de resposta excedido",
    "connection_interrupted": "Conexão interrompida",
    "response_interrupted": "Resposta interrompida",
    "transport_failed": "Falha de comunicação com a página",
    "tls_certificate_failed": "Certificado TLS não validado",
    "tls_failed": "Conexão TLS não estabelecida",
    "execution_time_limit": "Tempo máximo de análise atingido",
    "invalid_content_length": "Tamanho de resposta inválido",
    "response_too_large": "Resposta acima do limite permitido",
    "compressed_response_not_allowed": "Resposta comprimida não aceita",
    "redirect_loop": "Ciclo de redirecionamento identificado",
    "redirect_without_location": "Redirecionamento sem destino válido",
    "redirect_limit_reached": "Limite de redirecionamentos atingido",
    "unexpected_content_type": "Conteúdo não identificado como HTML",
    "invalid_html": "HTML não pôde ser interpretado",
    "html_title_missing": "Título HTML ausente",
    "html_title_multiple": "Múltiplos títulos HTML",
    "meta_description_missing": "Meta description ausente",
    "h1_missing": "Título principal H1 ausente",
    "canonical_missing": "Canonical ausente",
    "canonical_multiple": "Múltiplas URLs canonical",
    "canonical_differs": "Canonical diferente da URL analisada",
    "noindex_observed": "Diretiva noindex identificada",
    "json_ld_invalid": "JSON-LD inválido",
    "citation_title_missing": "Título acadêmico não identificado",
    "citation_author_missing": "Autoria acadêmica não identificada",
    "citation_journal_title_missing": "Título do periódico não identificado",
    "citation_publication_date_missing": "Data de publicação não identificada",
    "citation_doi_missing": "DOI acadêmico não identificado",
    "title_oai_html_divergence": "Divergência de título",
    "authors_oai_html_divergence": "Divergência de autoria",
    "publication_date_oai_html_divergence": "Divergência de data de publicação",
    "doi_oai_html_divergence": "Divergência de DOI",
    "journal_oai_html_divergence": "Divergência de periódico",
}


def html_issue_message(code: object, fallback: object = None) -> str:
    """Retorna mensagem pública no contexto HTML, inclusive para histórico legado.

    A camada de transporte é reutilizada do cliente OAI-PMH para preservar
    pinagem de DNS, TLS e limites; seus erros não podem, porém, sugerir que a
    análise de uma página HTML acessou um endpoint OAI-PMH.
    """
    if isinstance(code, str) and code in _HTML_TRANSPORT_MESSAGES:
        return _HTML_TRANSPORT_MESSAGES[code]
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return "Não foi possível verificar a página HTML com segurança."


def html_finding_label(code: object) -> str:
    """Traduz um identificador persistido sem alterar sua semântica interna."""
    if not isinstance(code, str):
        return "Achado técnico não classificado"
    if code in _HTML_FINDING_LABELS:
        return _HTML_FINDING_LABELS[code]
    if code == "http_unavailable":
        return "Resposta HTTP não verificada"
    match = re.fullmatch(r"http_(\d{3})", code)
    if match is not None:
        return f"Resposta HTTP {match.group(1)}"
    return "Achado técnico não classificado"


@dataclass(frozen=True, slots=True)
class HtmlClientConfig:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    max_execution_seconds: float = 30.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_redirects: int = 3
    max_request_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    max_retry_delay_seconds: float = 5.0
    min_request_interval_seconds: float = 0.5

    def __post_init__(self) -> None:
        if any(value <= 0 for value in (
            self.connect_timeout_seconds, self.read_timeout_seconds, self.max_execution_seconds,
            self.max_response_bytes, self.max_request_attempts, self.retry_backoff_seconds,
            self.max_retry_delay_seconds,
        )):
            raise ValueError("Os limites de análise HTML devem ser positivos.")
        if self.max_redirects < 0 or self.min_request_interval_seconds < 0:
            raise ValueError("Os limites de redirecionamento e intervalo são inválidos.")


@dataclass(frozen=True, slots=True)
class ValidatedHtmlUrl:
    canonical_url: str
    host: str
    port: int
    target: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HtmlHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HtmlHttpTransport(Protocol):
    def get(self, url: ValidatedHtmlUrl, config: HtmlClientConfig) -> HtmlHttpResponse:
        """Obtém uma resposta limitada, sem seguir redirects por conta própria."""


@dataclass(frozen=True, slots=True)
class HtmlFetchResult:
    source_url: str
    final_url: str | None
    http_status: int | None
    headers: Mapping[str, str]
    body: bytes | None
    redirect_count: int
    elapsed_ms: int
    issue_code: str | None = None
    issue_message: str | None = None


@dataclass(frozen=True, slots=True)
class HtmlFinding:
    code: str
    classification: str
    severity: str
    evidence: str
    recommendation: str
    applicability: str = "applicable"
    impact: str = "O impacto depende do contexto editorial e técnico da página."
    audience: str = "Equipe editorial e administração técnica do OJS."


@dataclass(frozen=True, slots=True)
class HtmlAnalysisResult:
    status: str
    source_url: str
    final_url: str | None
    http_status: int | None
    content_type: str | None
    response_time_ms: int | None
    redirect_count: int
    facts: dict
    findings: tuple[HtmlFinding, ...]
    evidence_hash: str | None

    @property
    def confirmed_problem_count(self) -> int:
        return sum(item.classification == "confirmed" for item in self.findings)

    @property
    def potential_problem_count(self) -> int:
        return sum(item.classification == "potential" for item in self.findings)

    @property
    def unverified_count(self) -> int:
        return sum(item.classification == "unverified" for item in self.findings)


def validate_public_html_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> ValidatedHtmlUrl:
    """Aceita somente URL HTTPS pública, sem credenciais e com DNS fixado."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2_048 or any(ord(char) < 32 for char in url):
        raise EndpointValidationError("invalid_html_url", "A URL pública do artigo é inválida.")
    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise EndpointValidationError("invalid_html_url", "A URL pública do artigo é inválida.") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise EndpointValidationError("https_required", "A página pública do artigo deve utilizar HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointValidationError("embedded_credentials", "A URL pública não pode conter credenciais.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise EndpointValidationError("invalid_html_port", "A porta da URL pública é inválida.") from exc
    if port not in (None, 443):
        raise EndpointValidationError("https_port_required", "A página pública deve utilizar a porta HTTPS.")
    raw_host = parsed.hostname.rstrip(".")
    try:
        ipaddress.ip_address(raw_host)
    except ValueError:
        pass
    else:
        raise EndpointValidationError("ip_literal_not_allowed", "Informe um host DNS público, não um endereço IP literal.")
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise EndpointValidationError("invalid_host", "O host da URL pública é inválido.") from exc
    path = parsed.path or "/"
    if not path.startswith("/") or "/../" in f"{path}/":
        raise EndpointValidationError("invalid_html_path", "O caminho da URL pública é inválido.")
    try:
        resolved = resolver(host, 443, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise EndpointValidationError("dns_resolution_failed", "Não foi possível validar publicamente o host informado.") from exc
    addresses: list[str] = []
    for item in resolved:
        try:
            address = item[4][0]
        except (IndexError, TypeError, KeyError):
            continue
        try:
            public = ipaddress.ip_address(address).is_global
        except ValueError:
            public = False
        if not public:
            raise EndpointValidationError(
                "non_public_dns_address", "O host não pode apontar para endereços locais, privados ou reservados."
            )
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise EndpointValidationError("empty_dns_answer", "A resolução do host não forneceu endereços públicos utilizáveis.")
    target = path + (f"?{parsed.query}" if parsed.query else "")
    return ValidatedHtmlUrl(urlunsplit(("https", host, path, parsed.query, "")), host, 443, target, tuple(addresses))


class PinnedHtmlTransport:
    """GET HTML com IP já validado, TLS normal e sem proxies do ambiente."""

    def __init__(self, *, connection_factory=None, ssl_context: ssl.SSLContext | None = None) -> None:
        self._connection_factory = connection_factory or _PinnedHTTPSConnection
        self._ssl_context = ssl_context or ssl.create_default_context()

    def get(self, url: ValidatedHtmlUrl, config: HtmlClientConfig) -> HtmlHttpResponse:
        deadline = time.monotonic() + config.max_execution_seconds
        last_error: OaiTransportError | None = None
        for address in url.addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OaiTransportError("execution_time_limit", "A análise HTML atingiu o tempo máximo permitido.")
            connection = self._connection_factory(
                url.host, address, url.port, min(config.connect_timeout_seconds, remaining), self._ssl_context
            )
            phase = "connect"
            try:
                connection.request("GET", url.target, headers={
                    "Host": url.host,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.1",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": DEFAULT_USER_AGENT,
                })
                if getattr(connection, "sock", None) is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OaiTransportError("execution_time_limit", "A análise HTML atingiu o tempo máximo permitido.")
                    connection.sock.settimeout(min(config.read_timeout_seconds, remaining))
                phase = "read"
                response = connection.getresponse()
                headers = {name.lower(): value for name, value in response.getheaders()}
                if headers.get("content-encoding", "identity").lower().strip() not in ("", "identity"):
                    raise OaiTransportError("compressed_response_not_allowed", "A página retornou uma codificação não aceita para a análise segura.")
                _enforce_content_length(headers.get("content-length"), config.max_response_bytes)
                # A resposta de redirect só fornece ``Location``; não lemos um
                # corpo opcional antes de revalidar o próximo destino.
                if 300 <= response.status < 400:
                    return HtmlHttpResponse(response.status, headers, b"")
                body = _read_limited_body(response, config.max_response_bytes, deadline=deadline)
                return HtmlHttpResponse(response.status, headers, body)
            except OaiClientError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
                last_error = _transport_error_from_exception(exc, phase=phase)
                if last_error.code.startswith("tls_"):
                    raise last_error from exc
            finally:
                connection.close()
        assert last_error is not None
        raise last_error


class HtmlPageClient:
    """Fetcher HTML com retries limitados e revalidação de cada redirect."""

    def __init__(
        self, *, config: HtmlClientConfig | None = None, resolver: Resolver = socket.getaddrinfo,
        transport: HtmlHttpTransport | None = None, clock: Clock = time.monotonic, sleeper: Sleeper = time.sleep,
    ) -> None:
        self.config = config or HtmlClientConfig()
        self._resolver = resolver
        self._transport = transport or PinnedHtmlTransport()
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None

    def fetch(self, source_url: str, *, deadline: float | None = None) -> HtmlFetchResult:
        started = self._clock()
        end = deadline if deadline is not None else started + self.config.max_execution_seconds
        current = source_url
        redirects = 0
        seen: set[str] = set()
        try:
            while True:
                validated = validate_public_html_url(current, resolver=self._resolver)
                if validated.canonical_url in seen:
                    raise OaiTransportError("redirect_loop", "A página redirecionou em ciclo e não pôde ser analisada com segurança.")
                seen.add(validated.canonical_url)
                response = self._request(validated, deadline=end)
                if 300 <= response.status < 400:
                    location = response.headers.get("location")
                    if not location:
                        raise OaiTransportError("redirect_without_location", "A página retornou um redirecionamento sem destino válido.")
                    if redirects >= self.config.max_redirects:
                        raise OaiTransportError("redirect_limit_reached", "A página excedeu o limite seguro de redirecionamentos.")
                    current = urljoin(validated.canonical_url, location)
                    redirects += 1
                    continue
                return HtmlFetchResult(
                    source_url, validated.canonical_url, response.status, response.headers, response.body,
                    redirects, self._elapsed_ms(started),
                )
        except OaiClientError as exc:
            return HtmlFetchResult(
                source_url,
                None,
                None,
                {},
                None,
                redirects,
                self._elapsed_ms(started),
                exc.code,
                html_issue_message(exc.code, exc.safe_message),
            )

    def _request(self, url: ValidatedHtmlUrl, *, deadline: float) -> HtmlHttpResponse:
        for attempt in range(1, self.config.max_request_attempts + 1):
            self._wait_for_interval(deadline)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise OaiTransportError("execution_time_limit", "A análise HTML atingiu o tempo máximo permitido.")
            config = replace(
                self.config,
                connect_timeout_seconds=min(self.config.connect_timeout_seconds, remaining),
                read_timeout_seconds=min(self.config.read_timeout_seconds, remaining),
                max_execution_seconds=min(self.config.max_execution_seconds, remaining),
            )
            try:
                response = self._transport.get(url, config)
                self._last_request_at = self._clock()
                return response
            except OaiClientError as error:
                self._last_request_at = self._clock()
                if not _is_retryable_transport_error(error) or attempt >= self.config.max_request_attempts:
                    raise
                requested = error.retry_after_seconds if isinstance(error, OaiTransportError) else None
                if requested is not None and requested > self.config.max_retry_delay_seconds:
                    raise
                delay = max(self.config.retry_backoff_seconds * (2 ** (attempt - 1)), requested or 0.0)
                if self._clock() + delay >= deadline:
                    raise OaiTransportError("execution_time_limit", "A análise HTML atingiu o tempo máximo permitido.")
                self._sleeper(delay)

    def _wait_for_interval(self, deadline: float) -> None:
        if self._last_request_at is None or not self.config.min_request_interval_seconds:
            return
        delay = self.config.min_request_interval_seconds - (self._clock() - self._last_request_at)
        if delay > 0:
            if self._clock() + delay >= deadline:
                raise OaiTransportError("execution_time_limit", "A análise HTML atingiu o tempo máximo permitido.")
            self._sleeper(delay)

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.h1: list[str] = []
        self.meta: dict[str, list[str]] = {}
        self.canonical: list[str] = []
        self.json_ld: list[str] = []
        self._active: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        lowered = tag.lower()
        if lowered in {"title", "h1"}:
            self._active, self._buffer = lowered, []
        elif lowered == "meta":
            name = (values.get("name") or values.get("property") or values.get("http-equiv") or "").strip().lower()
            content = values.get("content", "").strip()
            if name and content:
                self.meta.setdefault(name, []).append(_short(content, 2_000))
        elif lowered == "link" and "canonical" in {part.strip().lower() for part in values.get("rel", "").split()}:
            href = values.get("href", "").strip()
            if href:
                self.canonical.append(_short(href, 2_048))
        elif lowered == "script" and values.get("type", "").strip().lower() == "application/ld+json":
            self._active, self._buffer = "json_ld", []

    def handle_data(self, data: str) -> None:
        if self._active is not None and len("".join(self._buffer)) < 262_144:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active == tag.lower() or (self._active == "json_ld" and tag.lower() == "script"):
            content = " ".join("".join(self._buffer).split())
            if content:
                if self._active == "title":
                    self.title.append(_short(content, 2_000))
                elif self._active == "h1":
                    self.h1.append(_short(content, 2_000))
                else:
                    self.json_ld.append(content[:262_144])
            self._active, self._buffer = None, []


def _short(value: str, maximum: int) -> str:
    return " ".join(value.split())[:maximum]


def _normal(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", unescape(value))
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", normalized)
        if not unicodedata.combining(character)
    ).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", folded, flags=re.UNICODE).split())


def _doi(value: str) -> str:
    """Converte representações usuais de DOI para uma chave comparável."""
    candidate = unescape(value).strip()
    candidate = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"^doi\s*:\s*", "", candidate, flags=re.IGNORECASE)
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", candidate, flags=re.IGNORECASE)
    return (match.group(0) if match else candidate).rstrip(".,;").casefold()


def _evidence_values(values: Sequence[object]) -> tuple[str, ...]:
    """Mantém valores recebidos, limitados e sem transformar a evidência."""
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()[:2_000]
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _normalized_values(values: Sequence[str], normalizer: Callable[[str], str] = _normal) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = normalizer(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


_AUTHOR_NON_PERSON_MARKERS = frozenset({
    "academia", "centro", "centre", "department", "departamento", "editorial",
    "faculdade", "faculty", "grupo", "group", "instituto", "institute", "journal",
    "laboratorio", "laboratory", "programa", "program", "revista", "school",
    "universidade", "university",
})


def _normal_author_segment(value: str) -> str:
    """Normaliza um segmento de nome, unindo apóstrofos internos ao sobrenome."""
    normalized = unicodedata.normalize("NFKC", unescape(value))
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", normalized)
        if not unicodedata.combining(character)
    ).casefold()
    # O'Connor e D'Ávila são grafias de uma única unidade de sobrenome; hífen
    # permanece separador para manter equivalência com a forma espaçada.
    folded = re.sub(r"(?<=\w)['’](?=\w)", "", folded, flags=re.UNICODE)
    return " ".join(re.sub(r"[\W_]+", " ", folded, flags=re.UNICODE).split())


def _normalized_author(value: str) -> tuple[str, bool]:
    """Normaliza um nome e reconhece somente o formato bibliográfico explícito.

    A inversão é deliberadamente limitada a ``Sobrenome, Prenome`` com uma
    única vírgula e segmentos plausíveis de pessoa. Não reordena palavras de
    nomes que não trazem esse marcador nem sufixos como ``Jr.`` com vírgulas
    adicionais; assim, não usa permutação como prova de identidade.
    """
    raw = unicodedata.normalize("NFKC", unescape(value)).strip()
    if raw.count(",") != 1:
        return _normal_author_segment(raw), False
    surname, given_names = (part.strip() for part in raw.split(",", 1))
    surname_normalized = _normal_author_segment(surname)
    given_normalized = _normal_author_segment(given_names)
    surname_words = surname_normalized.split()
    given_words = given_normalized.split()
    if not surname_words or not given_words:
        return _normal_author_segment(raw), False
    if (
        len(surname_words) > 6
        or len(given_words) > 6
        or not all(word.isalpha() for word in (*surname_words, *given_words))
        or _AUTHOR_NON_PERSON_MARKERS.intersection((*surname_words, *given_words))
    ):
        return _normal_author_segment(raw), False
    return " ".join((*given_words, *surname_words)), True


def _normalized_authors(values: Sequence[str]) -> tuple[tuple[str, ...], bool]:
    normalized: list[str] = []
    used_bibliographic_inversion = False
    for value in values:
        candidate, inverted = _normalized_author(value)
        if candidate and candidate not in normalized:
            normalized.append(candidate)
        used_bibliographic_inversion = used_bibliographic_inversion or inverted
    return tuple(normalized), used_bibliographic_inversion


_BIBLIOGRAPHIC_DATE_RE = re.compile(
    r"^\s*(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?(?:[Tt\s].*)?\s*$"
)


def _normalized_bibliographic_date(value: str) -> str | None:
    """Aceita ano, ano-mês e data completa sem inventar precisão ausente."""
    match = _BIBLIOGRAPHIC_DATE_RE.match(unescape(value))
    if match is None:
        return None
    year, month, day = match.groups()
    if month is not None and not 1 <= int(month) <= 12:
        return None
    if day is not None and not 1 <= int(day) <= 31:
        return None
    return "-".join((year, *(part.zfill(2) for part in (month, day) if part is not None)))


def _comparison_row(
    *,
    field: str,
    label: str,
    oai_label: str,
    html_label: str,
    oai_values: Sequence[str],
    html_values: Sequence[str],
    oai_normalized: Sequence[str],
    html_normalized: Sequence[str],
    result: str,
    reason: str,
    recommendation: str,
    severity: str = "informative",
    applicability: str = "applicable",
) -> dict[str, object]:
    """Estrutura serializável da evidência de uma comparação individual."""
    return {
        "field": field,
        "label": label,
        "oai_label": oai_label,
        "html_label": html_label,
        "oai_values": tuple(oai_values),
        "html_values": tuple(html_values),
        "oai_normalized": tuple(oai_normalized),
        "html_normalized": tuple(html_normalized),
        "result": result,
        "reason": reason,
        "recommendation": recommendation,
        "severity": severity,
        "applicability": applicability,
    }


def _schema_types(value: object, output: set[str]) -> None:
    if isinstance(value, dict):
        raw_type = value.get("@type")
        if isinstance(raw_type, str):
            output.add(_short(raw_type, 100))
        elif isinstance(raw_type, list):
            output.update(_short(item, 100) for item in raw_type if isinstance(item, str))
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _schema_types(item, output)
    elif isinstance(value, list):
        for item in value:
            _schema_types(item, output)


def analyze_html_page(
    fetched: HtmlFetchResult,
    *,
    oai_fields: Mapping[str, Sequence[str]],
    oai_doi: str | None,
    repository_name: str | None,
    oai_dois: Sequence[str] = (),
) -> HtmlAnalysisResult:
    """Extrai somente evidências limitadas, sem inferir indexação externa."""
    if fetched.body is None:
        finding = HtmlFinding(
            fetched.issue_code or "html_unavailable", "unverified", "info",
            fetched.issue_message or "A página não pôde ser verificada.",
            "Verifique a disponibilidade pública da página e execute uma nova análise quando ela estiver acessível.",
            applicability="not_verified",
            impact="Como o HTML não foi recuperado, não é possível concluir a presença ou a ausência de metadados.",
        )
        return HtmlAnalysisResult(
            "not_verified", fetched.source_url, fetched.final_url, fetched.http_status, None,
            fetched.elapsed_ms, fetched.redirect_count,
            {"http": {"status": fetched.http_status, "redirect_count": fetched.redirect_count}}, (finding,), None,
        )
    content_type = _short(fetched.headers.get("content-type", "").split(";", 1)[0].strip().lower(), 200) or None
    facts: dict = {"http": {"status": fetched.http_status, "content_type": content_type, "redirect_count": fetched.redirect_count}}
    findings: list[HtmlFinding] = []
    if fetched.http_status is None or not 200 <= fetched.http_status < 300:
        findings.append(HtmlFinding(
            f"http_{fetched.http_status or 'unavailable'}", "confirmed", "high",
            f"A resposta HTTP observada foi {fetched.http_status or 'indisponível'}.",
            "Verifique a disponibilidade pública e as regras de acesso da página do artigo.",
        ))
        return HtmlAnalysisResult(
            "analyzed", fetched.source_url, fetched.final_url, fetched.http_status, content_type,
            fetched.elapsed_ms, fetched.redirect_count, facts, tuple(findings), sha256(fetched.body).hexdigest(),
        )
    if content_type not in {"text/html", "application/xhtml+xml", None}:
        findings.append(HtmlFinding(
            "unexpected_content_type", "unverified", "info",
            f"A resposta informou o tipo de conteúdo {content_type}.",
            "Verifique se a URL identificada corresponde a uma página pública HTML do artigo.",
            applicability="not_verified",
            impact="O conteúdo recebido não permitiu uma análise técnica HTML confiável.",
        ))
        return HtmlAnalysisResult(
            "not_verified", fetched.source_url, fetched.final_url, fetched.http_status, content_type,
            fetched.elapsed_ms, fetched.redirect_count, facts, tuple(findings), sha256(fetched.body).hexdigest(),
        )
    parser = _MetadataParser()
    try:
        parser.feed(fetched.body.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, UnicodeError) as exc:
        findings.append(HtmlFinding(
            "invalid_html", "unverified", "info", "O HTML não pôde ser interpretado de forma segura.",
            "Verifique a resposta pública e execute uma nova análise após corrigir a página.",
            applicability="not_verified",
            impact="O conteúdo recuperado não oferece base suficiente para concluir a presença ou ausência de metadados.",
        ))
        return HtmlAnalysisResult("not_verified", fetched.source_url, fetched.final_url, fetched.http_status, content_type,
                                  fetched.elapsed_ms, fetched.redirect_count, facts, tuple(findings), sha256(fetched.body).hexdigest())

    citation_fields = (
        "citation_title", "citation_author", "citation_journal_title", "citation_publication_date", "citation_issn",
        "citation_volume", "citation_issue", "citation_firstpage", "citation_lastpage", "citation_doi", "citation_pdf_url",
    )
    citations = {key: tuple(parser.meta.get(key, ())) for key in citation_fields}
    schema_types: set[str] = set()
    invalid_json_ld = 0
    for block in parser.json_ld:
        try:
            _schema_types(json.loads(block), schema_types)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_json_ld += 1
    header_x_robots = _short(fetched.headers.get("x-robots-tag", ""), 2_000)
    facts.update({
        "basic": {"title": tuple(parser.title), "description": tuple(parser.meta.get("description", ())), "h1": tuple(parser.h1),
                  "canonical": tuple(parser.canonical), "robots": tuple(parser.meta.get("robots", ())),
                  "x_robots_tag": tuple(value for value in (*parser.meta.get("x-robots-tag", ()), header_x_robots) if value)},
        "citation": citations,
        "structured_data": {"json_ld_blocks": len(parser.json_ld), "invalid_json_ld_blocks": invalid_json_ld, "schema_types": tuple(sorted(schema_types))},
        "sharing": {"open_graph_present": any(key.startswith("og:") for key in parser.meta), "twitter_card_present": any(key.startswith("twitter:") for key in parser.meta)},
    })
    if not parser.title:
        findings.append(HtmlFinding("html_title_missing", "confirmed", "medium", "Nenhum elemento title foi identificado na resposta HTML.", "Inclua um título HTML específico e descritivo para o artigo."))
    elif len(parser.title) > 1:
        findings.append(HtmlFinding("html_title_multiple", "potential", "low", "A resposta HTML contém mais de um elemento title.", "Revise a estrutura do cabeçalho HTML para manter um único título documental."))
    if not parser.meta.get("description"):
        findings.append(HtmlFinding(
            "meta_description_missing", "potential", "low",
            "Nenhuma meta description foi identificada na resposta HTML.",
            "Avalie a exposição de uma descrição resumida da página do artigo.",
            applicability="context_dependent",
            impact="A ausência é observável, mas o impacto depende do tema e da política editorial.",
        ))
    if not parser.h1:
        findings.append(HtmlFinding(
            "h1_missing", "potential", "low",
            "Nenhum elemento H1 foi identificado na resposta HTML.",
            "Verifique se a página apresenta um título principal semanticamente marcado.",
            applicability="context_dependent",
            impact="A estrutura semântica da página precisa de conferência no contexto do tema utilizado.",
        ))
    if not parser.canonical:
        findings.append(HtmlFinding(
            "canonical_missing", "confirmed", "medium",
            "Nenhum link canonical foi identificado na resposta HTML.",
            "Configure uma URL canonical apropriada para a página pública do artigo.",
            applicability="context_dependent",
            impact="A ausência é confirmada nesta resposta; a consequência depende de como outras versões da página são expostas.",
        ))
    elif len(parser.canonical) > 1:
        findings.append(HtmlFinding("canonical_multiple", "potential", "medium", "A resposta HTML contém mais de um link canonical.", "Mantenha uma única URL canonical para evitar sinais contraditórios."))
    elif fetched.final_url and all(
        _normal(urljoin(fetched.final_url, value)) != _normal(fetched.final_url) for value in parser.canonical
    ):
        findings.append(HtmlFinding("canonical_differs", "potential", "low", "O canonical observado é diferente da URL final analisada.", "Revise se a divergência é intencional e se aponta para a representação canônica do mesmo artigo."))
    robots = " ".join((*parser.meta.get("robots", ()), *parser.meta.get("x-robots-tag", ()), header_x_robots)).casefold()
    if "noindex" in robots:
        findings.append(HtmlFinding(
            "noindex_observed", "confirmed", "medium",
            "A resposta HTML ou o cabeçalho X-Robots-Tag contém noindex.",
            "Revise a diretiva de indexação se a página deve permanecer indexável.",
            applicability="context_dependent",
            impact="A diretiva é confirmada, mas pode ser intencional para determinados tipos de conteúdo.",
        ))
    if invalid_json_ld:
        findings.append(HtmlFinding("json_ld_invalid", "confirmed", "low", "Ao menos um bloco JSON-LD presente não possui sintaxe JSON válida.", "Corrija a sintaxe dos blocos JSON-LD antes de publicar metadados estruturados."))

    for required in ("citation_title", "citation_author", "citation_journal_title", "citation_publication_date"):
        if not citations[required]:
            findings.append(HtmlFinding(
                f"{required}_missing", "potential", "low", f"A metatag {required} não foi identificada na resposta HTML.",
                "Verifique a configuração de metadados acadêmicos exposta pelo tema e pela versão do OJS.",
                applicability="context_dependent",
                impact="A ausência da metatag é observável, mas não comprova indexação ausente nem uma falha da página em outros contextos.",
            ))
    if not citations["citation_doi"]:
        findings.append(HtmlFinding(
            "citation_doi_missing", "potential", "low", "A metatag citation_doi não foi identificada na resposta HTML.",
            "Se o artigo possuir DOI, verifique se ele está informado e exposto nos metadados acadêmicos do OJS.",
            applicability="context_dependent",
            impact="A ausência da metatag não comprova que o artigo não possua DOI.",
        ))

    _append_consistency_findings(
        findings, facts, citations, oai_fields, oai_doi, repository_name, oai_dois,
    )
    return HtmlAnalysisResult(
        "analyzed", fetched.source_url, fetched.final_url, fetched.http_status, content_type,
        fetched.elapsed_ms, fetched.redirect_count, facts, tuple(findings), sha256(fetched.body).hexdigest(),
    )


_COMPARISON_CRITERIA_VERSION = "ojs-html-comparison-v3"


def _insufficient_comparison(
    *, field: str, label: str, oai_label: str, html_label: str,
    oai_values: Sequence[str], html_values: Sequence[str],
    oai_normalized: Sequence[str] = (), html_normalized: Sequence[str] = (),
) -> dict[str, object]:
    if not oai_values and not html_values:
        reason = "Nenhum valor comparável foi identificado nas duas fontes."
    elif not oai_values:
        reason = f"Não há valor observável em {oai_label}; a comparação não tem base suficiente."
    else:
        reason = f"Não há valor observável em {html_label}; a comparação não tem base suficiente."
    return _comparison_row(
        field=field, label=label, oai_label=oai_label, html_label=html_label,
        oai_values=oai_values, html_values=html_values,
        oai_normalized=oai_normalized, html_normalized=html_normalized,
        result="not_verified", reason=reason,
        recommendation="Complete ou confirme os metadados nas duas fontes antes de concluir que há divergência.",
        applicability="not_verified",
    )


def _text_comparison(
    *, field: str, label: str, oai_label: str, html_label: str,
    oai_values: Sequence[str], html_values: Sequence[str],
) -> dict[str, object]:
    source = _evidence_values(oai_values)
    target = _evidence_values(html_values)
    source_normalized = _normalized_values(source)
    target_normalized = _normalized_values(target)
    if not source or not target:
        return _insufficient_comparison(
            field=field, label=label, oai_label=oai_label, html_label=html_label,
            oai_values=source, html_values=target,
            oai_normalized=source_normalized, html_normalized=target_normalized,
        )
    if set(source_normalized).intersection(target_normalized):
        reason = "Há ao menos um valor equivalente após normalização de Unicode, caixa, espaços, acentuação e pontuação."
        return _comparison_row(
            field=field, label=label, oai_label=oai_label, html_label=html_label,
            oai_values=source, html_values=target,
            oai_normalized=source_normalized, html_normalized=target_normalized,
            result="compatible", reason=reason,
            recommendation="Nenhuma ação é indicada por esta comparação.",
        )
    return _comparison_row(
        field=field, label=label, oai_label=oai_label, html_label=html_label,
        oai_values=source, html_values=target,
        oai_normalized=source_normalized, html_normalized=target_normalized,
        result="potential", severity="medium", applicability="context_dependent",
        reason="Os valores não coincidem após a normalização; a diferença exige conferência humana.",
        recommendation="Revise se os metadados do OAI-PMH e da página HTML representam a mesma versão bibliográfica.",
    )


def _authors_comparison(oai_values: Sequence[str], html_values: Sequence[str]) -> dict[str, object]:
    source = _evidence_values(oai_values)
    target = _evidence_values(html_values)
    source_normalized, source_inverted = _normalized_authors(source)
    target_normalized, target_inverted = _normalized_authors(target)
    if not source or not target:
        return _insufficient_comparison(
            field="authors", label="Autoria", oai_label="dc:creator", html_label="citation_author",
            oai_values=source, html_values=target,
            oai_normalized=source_normalized, html_normalized=target_normalized,
        )
    if Counter(source_normalized) == Counter(target_normalized):
        reordered = source_normalized != target_normalized
        return _comparison_row(
            field="authors", label="Autoria", oai_label="dc:creator", html_label="citation_author",
            oai_values=source, html_values=target,
            oai_normalized=source_normalized, html_normalized=target_normalized,
            result="compatible",
            reason=(
                "Os mesmos autores foram identificados após converter o formato bibliográfico explícito “Sobrenome, Prenome” para “Prenome Sobrenome”; partículas e sobrenomes compostos foram preservados."
                if source_inverted or target_inverted else
                "Os mesmos autores foram identificados após normalização, em ordem diferente."
                if reordered else
                "Os autores coincidem após normalização de Unicode, caixa, espaços, acentuação e pontuação."
            ),
            recommendation="Nenhuma ação é indicada por esta comparação.",
        )
    return _comparison_row(
        field="authors", label="Autoria", oai_label="dc:creator", html_label="citation_author",
        oai_values=source, html_values=target,
        oai_normalized=source_normalized, html_normalized=target_normalized,
        result="potential", severity="medium", applicability="context_dependent",
        reason="As listas de autores não coincidem após normalização. Abreviações, inversões sem vírgula e sobrenomes compostos não são tratados como prova de identidade.",
        recommendation="Confira a autoria no cadastro do OJS e nas metatags citation_author antes de corrigir os registros.",
    )


def _dates_comparison(oai_values: Sequence[str], html_values: Sequence[str]) -> dict[str, object]:
    source = _evidence_values(oai_values)
    target = _evidence_values(html_values)
    source_normalized = tuple(value for value in (_normalized_bibliographic_date(item) for item in source) if value)
    target_normalized = tuple(value for value in (_normalized_bibliographic_date(item) for item in target) if value)
    if not source or not target or not source_normalized or not target_normalized:
        return _insufficient_comparison(
            field="publication_date", label="Data de publicação", oai_label="dc:date",
            html_label="citation_publication_date", oai_values=source, html_values=target,
            oai_normalized=source_normalized, html_normalized=target_normalized,
        )
    if set(source_normalized).intersection(target_normalized):
        reason = "As datas coincidem na mesma precisão disponível."
        result, severity, applicability = "compatible", "informative", "applicable"
        recommendation = "Nenhuma ação é indicada por esta comparação."
    else:
        shared_precision = max(
            (
                min(len(left.split("-")), len(right.split("-")))
                for left in source_normalized
                for right in target_normalized
                if left.split("-")[:min(len(left.split("-")), len(right.split("-")))]
                == right.split("-")[:min(len(left.split("-")), len(right.split("-")))]
            ),
            default=0,
        )
        if shared_precision:
            label_by_precision = {1: "ano", 2: "ano e mês"}
            reason = (
                f"As datas são compatíveis no nível de {label_by_precision[shared_precision]}; "
                "a precisão menor não comprova igualdade da data completa."
            )
            result, severity, applicability = "compatible", "informative", "context_dependent"
            recommendation = "Confira a data completa somente se a precisão bibliográfica for relevante ao periódico."
        else:
            reason = "As datas não possuem uma parte comum na precisão disponível."
            result, severity, applicability = "potential", "low", "context_dependent"
            recommendation = "Revise a data de publicação no OJS e em citation_publication_date antes de corrigir os metadados."
    return _comparison_row(
        field="publication_date", label="Data de publicação", oai_label="dc:date",
        html_label="citation_publication_date", oai_values=source, html_values=target,
        oai_normalized=source_normalized, html_normalized=target_normalized,
        result=result, reason=reason, recommendation=recommendation,
        severity=severity, applicability=applicability,
    )


def _doi_comparison(
    oai_doi: str | None, oai_dois: Sequence[str], html_values: Sequence[str],
) -> dict[str, object]:
    source = _evidence_values(((oai_doi,) if oai_doi else ()) + tuple(oai_dois))
    target = _evidence_values(html_values)
    source_normalized = _normalized_values(source, _doi)
    target_normalized = _normalized_values(target, _doi)
    if not source or not target or not source_normalized or not target_normalized:
        return _insufficient_comparison(
            field="doi", label="DOI", oai_label="DOI identificado no OAI-PMH",
            html_label="citation_doi", oai_values=source, html_values=target,
            oai_normalized=source_normalized, html_normalized=target_normalized,
        )
    if set(source_normalized).intersection(target_normalized):
        result, reason, recommendation, severity, applicability = (
            "compatible",
            "As representações de DOI coincidem após remover o prefixo DOI e a URL de resolução.",
            "Nenhuma ação é indicada por esta comparação.",
            "informative",
            "applicable",
        )
    else:
        result, reason, recommendation, severity, applicability = (
            "potential",
            "Os DOIs identificados nas duas fontes não coincidem após normalização.",
            "Confirme a identificação bibliográfica do artigo antes de alterar metadados de DOI.",
            "high",
            "context_dependent",
        )
    return _comparison_row(
        field="doi", label="DOI", oai_label="DOI identificado no OAI-PMH", html_label="citation_doi",
        oai_values=source, html_values=target,
        oai_normalized=source_normalized, html_normalized=target_normalized,
        result=result, reason=reason, recommendation=recommendation,
        severity=severity, applicability=applicability,
    )


def _not_applicable_comparison(field: str, label: str, html_label: str, html_values: Sequence[str]) -> dict[str, object]:
    target = _evidence_values(html_values)
    return _comparison_row(
        field=field, label=label, oai_label="Evidência OAI-PMH disponível",
        html_label=html_label, oai_values=(), html_values=target,
        oai_normalized=(), html_normalized=_normalized_values(target),
        result="not_applicable",
        reason="A auditoria Dublin Core não preserva um campo específico e confiável para esta comparação.",
        recommendation="O valor HTML permanece disponível como evidência, mas não deve ser comparado sem uma fonte bibliográfica equivalente.",
        applicability="not_applicable",
    )


def _append_consistency_findings(
    findings: list[HtmlFinding], facts: dict, citations: Mapping[str, tuple[str, ...]],
    oai_fields: Mapping[str, Sequence[str]], oai_doi: str | None, repository_name: str | None,
    oai_dois: Sequence[str],
) -> None:
    comparisons = [
        _text_comparison(
            field="title", label="Título", oai_label="dc:title", html_label="citation_title",
            oai_values=oai_fields.get("title", ()), html_values=citations["citation_title"],
        ),
        _authors_comparison(oai_fields.get("creator", ()), citations["citation_author"]),
        _dates_comparison(oai_fields.get("date", ()), citations["citation_publication_date"]),
        _doi_comparison(oai_doi, oai_dois, citations["citation_doi"]),
        _text_comparison(
            field="journal", label="Periódico", oai_label="Nome do repositório OAI-PMH",
            html_label="citation_journal_title", oai_values=(repository_name,) if repository_name else (),
            html_values=citations["citation_journal_title"],
        ),
        _not_applicable_comparison("issn", "ISSN", "citation_issn", citations["citation_issn"]),
        _not_applicable_comparison("volume", "Volume", "citation_volume", citations["citation_volume"]),
        _not_applicable_comparison("issue", "Número", "citation_issue", citations["citation_issue"]),
        _not_applicable_comparison(
            "pages", "Paginação", "citation_firstpage / citation_lastpage",
            (*citations["citation_firstpage"], *citations["citation_lastpage"]),
        ),
    ]
    finding_rules = {
        "title": (
            "title_oai_html_divergence", "O título OAI-PMH e citation_title não coincidem após normalização.",
            "Revise se ambos representam a mesma versão bibliográfica do artigo.", "medium",
            "Diferenças de título podem dificultar a identificação consistente do mesmo registro.",
        ),
        "authors": (
            "authors_oai_html_divergence", "As listas de autoria OAI-PMH e citation_author não coincidem após normalização.",
            "Revise a consistência da autoria entre o cadastro OJS e os metadados expostos.", "medium",
            "Diferenças de autoria podem exigir correção editorial, mas abreviações requerem conferência humana.",
        ),
        "publication_date": (
            "publication_date_oai_html_divergence", "As datas OAI-PMH e citation_publication_date não coincidem na precisão disponível.",
            "Revise a data bibliográfica no OJS e a metatag antes de corrigir os registros.", "low",
            "A precisão de datas pode variar entre as fontes e depende do contexto editorial.",
        ),
        "doi": (
            "doi_oai_html_divergence", "O DOI identificado no OAI-PMH diverge de citation_doi na página HTML.",
            "Verifique a identificação do artigo antes de alterar metadados de DOI.", "high",
            "DOIs divergentes podem apontar para a identificação de registros bibliográficos diferentes.",
        ),
        "journal": (
            "journal_oai_html_divergence", "O nome do repositório OAI-PMH diverge de citation_journal_title.",
            "Confirme o título público do periódico e a configuração de metadados acadêmicos do OJS.", "medium",
            "A divergência pode ser uma abreviação legítima ou indicar metadados inconsistentes.",
        ),
    }
    for comparison in comparisons:
        if comparison["result"] != "potential" or comparison["field"] not in finding_rules:
            continue
        code, evidence, recommendation, severity, impact = finding_rules[comparison["field"]]
        findings.append(HtmlFinding(
            code, "potential", severity, evidence, recommendation,
            applicability="context_dependent", impact=impact,
            audience="Equipe editorial e administração técnica do OJS.",
        ))
    facts["oai_html_consistency"] = {
        "criteria_version": _COMPARISON_CRITERIA_VERSION,
        "comparisons": comparisons,
    }


__all__ = [
    "HtmlAnalysisResult", "HtmlClientConfig", "HtmlFetchResult", "HtmlFinding", "HtmlHttpResponse",
    "HtmlPageClient", "HtmlHttpTransport", "PinnedHtmlTransport", "ValidatedHtmlUrl", "analyze_html_page",
    "validate_public_html_url", "html_finding_label",
]
