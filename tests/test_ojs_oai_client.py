from __future__ import annotations

import ast
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from platform_core.ojs_oai_client import (
    EndpointValidationError,
    HttpResponse,
    OaiClientConfig,
    OaiPmhClient,
    OaiProtocolError,
    OaiTransportError,
    PinnedHttpsTransport,
    _PinnedHTTPSConnection,
    validate_oai_endpoint,
)


FIXTURES = Path(__file__).parent / "fixtures" / "ojs_oai"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, endpoint, params, config):
        self.calls.append((endpoint, dict(params), config))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class AdvancingClock:
    """Relógio monotônico determinístico para testar orçamento e backoff."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.value += seconds


def response(body: bytes, status: int = 200, **headers) -> HttpResponse:
    headers = {key.replace("_", "-").lower(): str(value) for key, value in headers.items()}
    return HttpResponse(status, headers, body)


def client_for(responses, **config_values):
    return OaiPmhClient(
        "https://journal.example.test/oai",
        resolver=public_resolver,
        transport=ScriptedTransport(responses),
        config=OaiClientConfig(min_request_interval_seconds=0, **config_values),
    )


class OaiPmhClientTests(unittest.TestCase):
    def test_identify_parses_version_independent_ojs_fixtures(self):
        for fixture_name, expected_name in (
            ("identify_ojs33.xml", "Fixture OJS 3.3"),
            ("identify_ojs34.xml", "Fixture OJS 3.4"),
            ("identify_ojs35.xml", "Fixture OJS 3.5"),
        ):
            with self.subTest(fixture=fixture_name):
                identity = client_for([response(fixture(fixture_name))]).identify()
                self.assertEqual(expected_name, identity.repository_name)
                self.assertEqual("2.0", identity.protocol_version)

    def test_identify_rejects_invalid_or_hostile_xml_without_exposing_content(self):
        for payload in (
            b"<OAI-PMH>",
            fixture("hostile_dtd.xml"),
            b"<not-oai><Identify /></not-oai>",
        ):
            with self.subTest(payload=payload[:16]):
                with self.assertRaises(OaiProtocolError) as raised:
                    client_for([response(payload)]).identify()
                self.assertIn(raised.exception.code, {"unsafe_or_invalid_xml", "invalid_oai_envelope"})
                self.assertNotIn("passwd", raised.exception.safe_message)

    def test_lists_metadata_formats_and_requires_oai_dc_for_audit(self):
        client = client_for([response(fixture("metadata_formats.xml"))])
        formats = client.list_metadata_formats()
        self.assertEqual(("oai_dc", "marcxml"), tuple(item.metadata_prefix for item in formats))

        no_dc = fixture("metadata_formats.xml").replace(b"oai_dc", b"mods", 1)
        audit = client_for([
            response(fixture("identify_ojs35.xml")), response(no_dc)
        ]).inspect()
        self.assertEqual("failed", audit.status)
        self.assertEqual("oai_dc_not_advertised", audit.errors[0].code)

    def test_collects_paged_records_tombstones_duplicates_multivalue_dc_and_dois(self):
        transport = ScriptedTransport([
            response(fixture("list_records_page_1.xml")),
            response(fixture("list_records_page_2.xml")),
        ])
        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
        )
        result = client.collect_oai_dc()
        self.assertEqual("completed", result.status)
        self.assertEqual(2, result.metrics["active_records"])
        self.assertEqual(1, result.metrics["deleted_records"])
        self.assertEqual(3, result.metrics["unique_identifiers"])
        self.assertEqual(("oai:test:article-1",), result.duplicate_identifiers)
        self.assertEqual({"verb": "ListRecords", "metadataPrefix": "oai_dc"}, transport.calls[0][1])
        self.assertEqual({"verb": "ListRecords", "resumptionToken": "next-token"}, transport.calls[1][1])
        first, second = result.records
        self.assertEqual(("Título um", "Title one"), first.fields["title"])
        self.assertEqual(("Ana", "Bruno"), first.fields["creator"])
        self.assertEqual(("10.1000/abc.1",), first.dois)
        self.assertEqual(("10.5555/second",), second.dois)
        self.assertEqual(2, result.field_coverage()["title"])
        self.assertEqual(1, result.field_coverage()["description"])

    def test_full_inspection_exposes_observations_and_derived_metrics_separately(self):
        audit = client_for([
            response(fixture("identify_ojs35.xml")),
            response(fixture("metadata_formats.xml")),
            response(fixture("list_records_page_1.xml")),
            response(fixture("list_records_page_2.xml")),
        ]).inspect()
        self.assertEqual("completed", audit.status)
        self.assertEqual("Fixture OJS 3.5", audit.identity.repository_name)
        self.assertEqual(("oai_dc", "marcxml"), tuple(item.metadata_prefix for item in audit.metadata_formats))
        self.assertEqual(
            {
                "active_records": 2,
                "deleted_records": 1,
                "records_with_normalized_metadata": 2,
                "records_with_doi": 2,
                "identified_dois": 2,
                "unique_identifiers": 3,
                "duplicate_identifiers": 1,
                "pages_fetched": 2,
                "total_response_bytes": audit.collection.total_response_bytes,
            },
            audit.collection.metrics,
        )
        self.assertGreaterEqual(audit.collection.elapsed_seconds, 0.0)

    def test_expired_oai_token_returns_partial_result(self):
        result = client_for([
            response(fixture("list_records_page_1.xml")),
            response(fixture("oai_error_bad_token.xml")),
        ]).collect_oai_dc()
        self.assertEqual("partial", result.status)
        self.assertEqual("oai_error_badResumptionToken", result.completion_reason)
        self.assertEqual(1, result.metrics["active_records"])
        self.assertNotIn("private diagnostic", result.errors[0].message)

    def test_active_record_without_metadata_is_retained_without_inventing_dc_values(self):
        payload = b'''<?xml version="1.0"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>
          <record><header><identifier>oai:test:metadata-absent</identifier><datestamp>2025-01-01</datestamp></header></record>
        </ListRecords></OAI-PMH>'''
        result = client_for([response(payload)]).collect_oai_dc()
        self.assertEqual("completed", result.status)
        self.assertEqual(1, result.metrics["active_records"])
        self.assertEqual(0, result.metrics["records_with_normalized_metadata"])
        self.assertFalse(result.records[0].metadata_present)
        self.assertEqual((), result.records[0].dois)
        self.assertTrue(all(not values for values in result.records[0].fields.values()))

    def test_http_200_oai_error_during_discovery_is_failed_without_response_body(self):
        audit = client_for([response(fixture("oai_error_bad_token.xml"))]).inspect()
        self.assertEqual("failed", audit.status)
        self.assertEqual("oai_error_badResumptionToken", audit.errors[0].code)
        self.assertNotIn("private diagnostic", audit.errors[0].message)

    def test_repeated_token_and_repeated_page_are_partial(self):
        repeated_token = fixture("list_records_page_1.xml").replace(b"next-token", b"loop-token")
        with_token = fixture("list_records_page_1.xml").replace(b"next-token", b"loop-token")
        result = client_for([response(repeated_token), response(with_token)]).collect_oai_dc()
        self.assertEqual("partial", result.status)
        self.assertEqual("repeated_page", result.completion_reason)

        changed_page = with_token.replace("Título um".encode("utf-8"), "Outro título".encode("utf-8"))
        result = client_for([response(with_token), response(changed_page)]).collect_oai_dc()
        self.assertEqual("partial", result.status)
        self.assertEqual("repeated_resumption_token", result.completion_reason)

    def test_http_errors_redirects_timeouts_and_interruption_are_safe(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                with self.assertRaises(OaiTransportError) as raised:
                    client_for([response(b"", status=status)]).identify()
                self.assertEqual(f"http_{status}", raised.exception.code)
        for status in (429, 500):
            with self.subTest(status=status):
                with self.assertRaises(OaiTransportError) as raised:
                    client_for([response(b"", status=status)] * 3).identify()
                self.assertEqual(f"http_{status}", raised.exception.code)
        with self.assertRaises(OaiTransportError) as raised:
            client_for([response(b"", status=302, location="https://elsewhere.example.test")]).identify()
        self.assertEqual("redirect_not_allowed", raised.exception.code)

        result = client_for([TimeoutError("hidden endpoint details")] * 3).collect_oai_dc()
        self.assertEqual("failed", result.status)
        self.assertEqual("connect_timeout", result.errors[0].code)
        self.assertNotIn("hidden", result.errors[0].message)

        result = client_for([
            OaiTransportError("response_interrupted", "Resposta interrompida.")
        ] * 3).collect_oai_dc()
        self.assertEqual("response_interrupted", result.completion_reason)

    def test_transient_transport_errors_retry_without_repeating_completed_pages(self):
        transport = ScriptedTransport([
            TimeoutError("connection details must stay private"),
            response(fixture("list_records_page_1.xml")),
            response(fixture("list_records_page_2.xml")),
        ])
        clock = AdvancingClock()
        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
            clock=clock,
            sleeper=clock.sleep,
        )
        result = client.collect_oai_dc()
        self.assertEqual("completed", result.status)
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(transport.calls[0][1], transport.calls[1][1])
        self.assertEqual({"verb": "ListRecords", "resumptionToken": "next-token"}, transport.calls[2][1])
        self.assertEqual([0.25], clock.pauses)

        transport = ScriptedTransport([
            ConnectionResetError("private connection state"),
            response(fixture("list_records_page_2.xml")),
        ])
        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
        )
        result = client.collect_oai_dc()
        self.assertEqual(("completed", 2), (result.status, result.metrics["active_records"]))
        self.assertEqual(2, len(transport.calls))

        transport = ScriptedTransport([
            response(b"", status=429, retry_after="1"),
            response(fixture("list_records_page_2.xml")),
        ])
        clock = AdvancingClock()
        result = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
            clock=clock,
            sleeper=clock.sleep,
        ).collect_oai_dc()
        self.assertEqual("completed", result.status)
        self.assertEqual([1.0], clock.pauses)

    def test_fourth_page_retry_reuses_token_and_does_not_duplicate_records(self):
        def page(number: int, next_token: str | None = None) -> bytes:
            token = f"<resumptionToken>{next_token}</resumptionToken>" if next_token else ""
            return f'''<?xml version="1.0" encoding="UTF-8"?>
            <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
              <ListRecords><record><header><identifier>oai:test:page-{number}</identifier><datestamp>2026-01-0{number}</datestamp></header>
              <metadata><dc:dc><dc:title>Página {number}</dc:title></dc:dc></metadata></record>{token}</ListRecords>
            </OAI-PMH>'''.encode("utf-8")

        transport = ScriptedTransport([
            response(page(1, "token-1")),
            response(page(2, "token-2")),
            response(page(3, "token-3")),
            OaiTransportError("response_interrupted", "Interrupção transitória."),
            response(page(4)),
        ])
        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
        )
        result = client.collect_oai_dc()
        self.assertEqual(("completed", 4, 4, 0), (
            result.status,
            result.metrics["pages_fetched"],
            result.metrics["unique_identifiers"],
            result.metrics["duplicate_identifiers"],
        ))
        self.assertEqual({"verb": "ListRecords", "resumptionToken": "token-3"}, transport.calls[3][1])
        self.assertEqual(transport.calls[3][1], transport.calls[4][1])

    def test_persistent_or_definitive_errors_do_not_corrupt_partial_results_or_retry_unsafe_cases(self):
        transport = ScriptedTransport([
            response(fixture("list_records_page_1.xml")),
            OaiTransportError("response_interrupted", "Interrupção transitória."),
            OaiTransportError("response_interrupted", "Interrupção transitória."),
            OaiTransportError("response_interrupted", "Interrupção transitória."),
        ])
        result = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
        ).collect_oai_dc()
        self.assertEqual(("partial", "response_interrupted", 1, 2), (
            result.status,
            result.completion_reason,
            result.metrics["pages_fetched"],
            result.metrics["unique_identifiers"],
        ))
        self.assertEqual(4, len(transport.calls))

        transport = ScriptedTransport([response(b"", status=404)])
        result = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
        ).collect_oai_dc()
        self.assertEqual(("failed", "http_404", 1), (result.status, result.completion_reason, len(transport.calls)))

        transport = ScriptedTransport([OaiTransportError("tls_certificate_failed", "TLS failure.")])
        result = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=transport,
            config=OaiClientConfig(min_request_interval_seconds=0),
        ).collect_oai_dc()
        self.assertEqual(("failed", "tls_certificate_failed", 1), (
            result.status, result.completion_reason, len(transport.calls)
        ))

    def test_retry_and_rate_waits_cannot_exceed_collection_budget(self):
        clock = AdvancingClock()

        class SlowFailingTransport(ScriptedTransport):
            def get(self, endpoint, params, config):
                clock.value += min(0.3, config.read_timeout_seconds)
                return super().get(endpoint, params, config)

        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=SlowFailingTransport([TimeoutError("temporary"), TimeoutError("temporary")]),
            config=OaiClientConfig(
                min_request_interval_seconds=0,
                max_execution_seconds=0.6,
            ),
            clock=clock,
            sleeper=clock.sleep,
        )
        result = client.collect_oai_dc()
        self.assertEqual(("failed", "execution_time_limit"), (result.status, result.completion_reason))
        self.assertEqual(2, len(client._transport.calls))
        self.assertLessEqual(result.elapsed_seconds, 0.6)

    def test_safety_limits_yield_partial_results_without_unbounded_collection(self):
        result = client_for([response(fixture("list_records_page_1.xml"))], max_pages=1).collect_oai_dc()
        self.assertEqual(("partial", "page_limit_reached"), (result.status, result.completion_reason))

        result = client_for([response(fixture("list_records_page_1.xml"))], max_records=1).collect_oai_dc()
        self.assertEqual(("partial", "record_limit_reached"), (result.status, result.completion_reason))

        page_one = fixture("list_records_page_1.xml")
        page_two = fixture("list_records_page_2.xml")
        result = client_for(
            [response(page_one), response(page_two)],
            max_response_bytes=max(len(page_one), len(page_two)) + 1,
            max_total_response_bytes=len(page_one) + len(page_two) - 1,
        ).collect_oai_dc()
        self.assertEqual(("partial", "total_response_size_limit"), (result.status, result.completion_reason))

        result = client_for([response(b"x" * 20)], max_response_bytes=10, max_total_response_bytes=10).collect_oai_dc()
        self.assertEqual(("failed", "response_too_large"), (result.status, result.completion_reason))

        result = client_for(
            [response(fixture("list_records_page_1.xml"))], max_resumption_token_length=4
        ).collect_oai_dc()
        self.assertEqual(("partial", "resumption_token_too_large"), (result.status, result.completion_reason))

    def test_response_headers_cannot_bypass_size_or_compression_controls(self):
        with self.assertRaises(OaiTransportError) as raised:
            client_for([response(fixture("identify_ojs33.xml"), content_encoding="gzip")]).identify()
        self.assertEqual("compressed_response_not_allowed", raised.exception.code)
        with self.assertRaises(OaiTransportError) as raised:
            client_for([response(fixture("identify_ojs33.xml"), content_length="not-a-number")]).identify()
        self.assertEqual("invalid_content_length", raised.exception.code)

    def test_execution_duration_and_minimum_interval_are_enforced(self):
        clock = AdvancingClock()

        class SlowResponseTransport(ScriptedTransport):
            def get(self, endpoint, params, config):
                clock.value += 2.0
                return super().get(endpoint, params, config)

        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=SlowResponseTransport([response(fixture("list_records_page_1.xml"))]),
            config=OaiClientConfig(min_request_interval_seconds=0, max_execution_seconds=1),
            clock=clock,
            sleeper=clock.sleep,
        )
        result = client.collect_oai_dc()
        self.assertEqual(("failed", "execution_time_limit"), (result.status, result.completion_reason))

        ticks = iter((0.0, 0.2, 0.2))
        pauses = []
        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=ScriptedTransport([response(fixture("identify_ojs33.xml")), response(fixture("metadata_formats.xml"))]),
            config=OaiClientConfig(min_request_interval_seconds=1),
            clock=lambda: next(ticks),
            sleeper=pauses.append,
        )
        client.identify()
        client.list_metadata_formats()
        self.assertEqual([0.8], pauses)

    def test_endpoint_validation_rejects_ssrf_vectors_and_keeps_only_public_dns(self):
        cases = (
            "http://journal.example.test/oai",
            "https://journal.example.test:444/oai",
            "https://user:password@journal.example.test/oai",
            "https://journal.example.test/oai?verb=Identify",
            "https://127.0.0.1/oai",
            "https://[::1]/oai",
        )
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(EndpointValidationError):
                    validate_oai_endpoint(url, resolver=public_resolver)

        for address in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "fc00::1"):
            with self.subTest(address=address):
                def resolver(*_args, address=address, **_kwargs):
                    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
                with self.assertRaises(EndpointValidationError):
                    validate_oai_endpoint("https://journal.example.test/oai", resolver=resolver)

        endpoint = validate_oai_endpoint("https://JOURNAL.example.test/oai", resolver=public_resolver)
        self.assertEqual("https://journal.example.test/oai", endpoint.canonical_url)
        self.assertEqual(("8.8.8.8",), endpoint.addresses)

        def mixed_resolver(*_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
        with self.assertRaises(EndpointValidationError) as raised:
            validate_oai_endpoint("https://journal.example.test/oai", resolver=mixed_resolver)
        self.assertEqual("non_public_dns_address", raised.exception.code)

    def test_pinned_connection_preserves_tls_hostname_and_proxy_environment_is_ignored(self):
        class RawSocket:
            def __init__(self):
                self.timeout = None
            def settimeout(self, value):
                self.timeout = value

        class TlsContext:
            def __init__(self):
                self.calls = []
            def wrap_socket(self, raw_socket, server_hostname):
                self.calls.append((raw_socket, server_hostname))
                return object()

        context = TlsContext()
        raw = RawSocket()
        with patch("platform_core.ojs_oai_client.socket.create_connection", return_value=raw) as create_connection:
            connection = _PinnedHTTPSConnection("journal.example.test", "8.8.8.8", 443, 3, context)
            connection.connect()
        create_connection.assert_called_once_with(("8.8.8.8", 443), 3)
        self.assertEqual("journal.example.test", context.calls[0][1])
        self.assertEqual(3, raw.timeout)

        captured = []
        class FakeResponse:
            status = 200
            def getheaders(self): return []
            def read(self, _size): return b""
        class FakeConnection:
            def __init__(self, host, pinned_ip, port, timeout, tls_context):
                captured.append((host, pinned_ip, port))
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return FakeResponse()
            def close(self): pass

        endpoint = validate_oai_endpoint("https://journal.example.test/oai", resolver=public_resolver)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9999"}, clear=False):
            PinnedHttpsTransport(connection_factory=FakeConnection).get(endpoint, {}, OaiClientConfig())
        self.assertEqual([("journal.example.test", "8.8.8.8", 443)], captured)

        class RefusingResponse(FakeResponse):
            status = 429
        class RefusingConnection(FakeConnection):
            def getresponse(self): return RefusingResponse()

        def two_public_addresses(*_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            ]
        endpoint = validate_oai_endpoint("https://journal.example.test/oai", resolver=two_public_addresses)
        captured.clear()
        with self.assertRaises(OaiTransportError) as raised:
            PinnedHttpsTransport(connection_factory=RefusingConnection).get(endpoint, {}, OaiClientConfig())
        self.assertEqual("http_429", raised.exception.code)
        self.assertEqual(1, len(captured))

    def test_module_is_independent_from_database_and_web_frameworks(self):
        source = (Path(__file__).parents[1] / "platform_core" / "ojs_oai_client.py").read_text(encoding="utf-8")
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("sqlalchemy", imported_roots)
        self.assertNotIn("flask", imported_roots)


if __name__ == "__main__":
    unittest.main()
