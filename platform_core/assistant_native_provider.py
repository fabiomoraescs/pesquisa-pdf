"""Provider local Análysis IA, sem SDK, rede, credencial ou modelo remoto.

Ele reaproveita o mesmo loop de tools autorizado do Assistente. A primeira
rodada formula uma chamada determinística; a continuação constrói uma resposta
extrativa/factual apenas a partir do resultado dessa chamada.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .assistant_ai_provider import AIProvider, AIProviderError
from .assistant_native_intent import NativeQuestionPlan, native_question_plan


DEFAULT_ANALYSIS_NATIVE_MODEL = "analysis-native-v1"
MAX_NATIVE_EXCERPT_CHARS = 900
MAX_NATIVE_EVIDENCE_ITEMS = 5


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _question_from_input(input_items: Sequence[Any]) -> str:
    for item in reversed(input_items):
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
    return ""


def _compact(value: object, limit: int = MAX_NATIVE_EXCERPT_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit].rstrip()


def _tool_arguments(plan: NativeQuestionPlan, question: str) -> dict[str, Any]:
    if plan.tool_name == "get_platform_help":
        return {"topic": plan.query or question[:200] or None}
    if plan.tool_name == "search_corpus":
        return {"query": plan.query[:200], "scope": "analysis", "document_ids": None, "retrieval_mode": "lexical"}
    if plan.tool_name == "read_document_pages":
        # A política só libera essa tool quando há um contexto autorizado; sem
        # identificador conhecido, a execução devolve limitação segura.
        return {"document_id": "", "start_page": 1, "end_page": 1}
    if plan.tool_name in {"summarize_document", "summarize_corpus"}:
        # O nativo não delega síntese a provider externo. A continuação trata
        # essa capacidade como limitada em vez de acionar o executor remoto.
        return {"question": question[:1000], **({"document_id": None} if plan.tool_name == "summarize_document" else {})}
    return {}


def _response_with_call(plan: NativeQuestionPlan, question: str) -> dict[str, Any]:
    if plan.tool_name is None:
        return {
            "output": [],
            "output_text": (
                "A Análysis IA não possui geração livre nem conhecimento externo. "
                "Posso responder quando a pergunta puder ser fundamentada na documentação interna, "
                "na tela atual, no projeto ou em evidências autorizadas do corpus."
            ),
            "native_trace": [{"source_type": "native_inference", "operation": plan.operation}],
        }
    call_id = "analysis-native-0"
    return {
        "output": [{
            "type": "function_call", "name": plan.tool_name, "call_id": call_id,
            "arguments": json.dumps(_tool_arguments(plan, question), ensure_ascii=False),
        }],
        "output_text": "",
        "native_plan": {
            "intent": plan.intent,
            "operation": plan.operation,
            "scope": plan.scope,
            "query": plan.query,
            "corpus_only": plan.corpus_only,
        },
    }


def _result_for_call(response: object, tool_outputs: Sequence[dict[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    calls = _mapping(response).get("output")
    names = {
        item.get("call_id"): item.get("name")
        for item in calls if isinstance(item, Mapping) and item.get("type") == "function_call"
    } if isinstance(calls, list) else {}
    parsed: list[tuple[str, Mapping[str, Any]]] = []
    for output in tool_outputs:
        call_id = output.get("call_id") if isinstance(output, Mapping) else None
        name = names.get(call_id)
        raw = output.get("output") if isinstance(output, Mapping) else ""
        try:
            result = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            result = {}
        if isinstance(name, str):
            parsed.append((name, _mapping(result)))
    return parsed


class EvidenceRetriever:
    """Fronteira do motor nativo para resultados já autorizados de tools."""

    @staticmethod
    def from_tool_outputs(response: object, tool_outputs: Sequence[dict[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
        return _result_for_call(response, tool_outputs)


class EvidenceRanker:
    """Mantém a ordem do recuperador canônico e apenas limita a apresentação."""

    @staticmethod
    def select(evidence: object) -> list[Mapping[str, Any]]:
        if not isinstance(evidence, list):
            return []
        return [_mapping(item) for item in evidence[:MAX_NATIVE_EVIDENCE_ITEMS] if isinstance(item, Mapping)]


class EvidenceAggregator:
    """Agrupa evidências por documento/página sem construir fatos novos."""

    @staticmethod
    def document_page_labels(evidence: Sequence[Mapping[str, Any]]) -> list[str]:
        labels: list[str] = []
        seen: set[tuple[str, object]] = set()
        for entry in evidence:
            name = _compact(entry.get("document_name"), 180) or "Documento"
            page = entry.get("page_number")
            key = (name, page)
            if key not in seen:
                labels.append(f"{name}, página {page}")
                seen.add(key)
        return labels


@dataclass(frozen=True)
class StructuredFinding:
    """Um achado extraído de uma evidência já autorizada, com proveniência."""

    kind: str
    text: str
    document_name: str
    page_number: int | None


@dataclass(frozen=True)
class StructuredFindings:
    """Resultado intermediário: fatos observáveis, não uma conclusão livre."""

    operation: str
    evidence: tuple[Mapping[str, Any], ...]
    findings: tuple[StructuredFinding, ...]
    limitations: tuple[str, ...]
    methods: tuple[str, ...]


def _sentences(text: object) -> list[str]:
    """Mantém unidades textuais completas; fragmentos isolados não viram prova."""
    compact = " ".join(str(text or "").split())
    return [sentence.strip() for sentence in re.findall(r"[^.!?]+[.!?]", compact)
            if 24 <= len(sentence.strip()) <= MAX_NATIVE_EXCERPT_CHARS]


def _source_label(document_name: object, page_number: object) -> str:
    name = _compact(document_name, 180) or "Documento"
    return f"{name}, página {page_number}" if isinstance(page_number, int) else name


class EvidenceAnalyzer:
    """Reconhece sinais textuais explícitos sem inferir além do trecho."""

    _PATTERNS = (
        ("definition", re.compile(r"\b(?:é|são|foi|foram)\b.*\b(?:compreendid|definid|construc|conceb|entendid)", re.I)),
        ("characteristic", re.compile(r"\b(?:caracteriz|marcad|constitui|apresenta|possui)\b", re.I)),
        ("relation", re.compile(r"\b(?:relacion|articul|vincul|conect|associ|dialog)\w*\b", re.I)),
        ("argument", re.compile(r"\b(?:argumenta|defende|sustenta|afirma|critica|contesta)\b", re.I)),
        ("example", re.compile(r"\b(?:por exemplo|como|tais como|a exemplo de)\b", re.I)),
    )

    def analyze(self, result: Mapping[str, Any], operation: str) -> StructuredFindings:
        evidence = tuple(EvidenceRanker.select(result.get("evidence")))
        findings: list[StructuredFinding] = []
        for entry in evidence:
            name = _compact(entry.get("document_name"), 180) or "Documento"
            page = entry.get("page_number")
            page_number = page if isinstance(page, int) else None
            for sentence in _sentences(entry.get("text") or entry.get("preview")):
                for kind, pattern in self._PATTERNS:
                    if pattern.search(sentence):
                        findings.append(StructuredFinding(kind, sentence, name, page_number))
                        break
        retrieval = _mapping(result.get("retrieval"))
        methods = tuple(str(item) for item in retrieval.get("methods", []) if isinstance(item, str))
        limitations = tuple(_compact(item, 360) for item in result.get("limitations", [])
                            if isinstance(item, str) and _compact(item, 360))
        return StructuredFindings(operation, evidence, tuple(findings), limitations, methods)


def _platform_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    documentation = _mapping(result.get("official_documentation"))
    page = _mapping(documentation.get("page"))
    sections = documentation.get("sections") if isinstance(documentation.get("sections"), list) else []
    access = _mapping(result.get("access"))
    tool_names = [str(item.get("name")) for item in access.get("available_tools", []) if isinstance(item, Mapping)]
    chunks = []
    if page.get("summary"):
        chunks.append(_compact(page.get("summary"), 420))
    for section in sections[:2]:
        if isinstance(section, Mapping):
            title, content = _compact(section.get("title"), 120), _compact(section.get("content"), 500)
            if content:
                chunks.append(f"{title}: {content}" if title else content)
    if tool_names:
        chunks.append("Ferramentas disponíveis para esta conta: " + ", ".join(tool_names) + ".")
    if not chunks:
        chunks.append("Não encontrei documentação interna suficiente para responder a essa pergunta.")
    return "\n\n".join(chunks), [{"source_type": "platform_documentation", "label": "Documentação interna"}]


def _ui_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    location = _compact(result.get("location"), 140) or "esta área"
    navigation = _compact(result.get("navigation"), 500)
    answer = f"Você está em {location}."
    if navigation:
        answer += " " + navigation
    return answer, [{"source_type": "project_state", "label": "Contexto atual da interface"}]


def _project_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    context = _mapping(result.get("context"))
    project = _mapping(context.get("project"))
    analysis = _mapping(_mapping(context.get("analysis")).get("selected"))
    corpus = _mapping(context.get("corpus"))
    chunks = []
    if project.get("name"):
        chunks.append(f"Projeto registrado: {_compact(project.get('name'), 200)}.")
    if analysis.get("display_name") or analysis.get("name"):
        chunks.append("Base selecionada: " + _compact(analysis.get("display_name") or analysis.get("name"), 200) + ".")
    documents = corpus.get("selected_analysis_document_count")
    if isinstance(documents, int):
        chunks.append(f"A Base selecionada possui {documents} registro(s) de documento.")
    operations = _mapping(context.get("operations"))
    for name, label, source_type in (
        ("codes", "código(s)", "coding"),
        ("codings", "codificação(ões)", "coding"),
        ("memos", "memo(s)", "memo"),
    ):
        total = _mapping(operations.get(name)).get("total")
        if isinstance(total, int):
            chunks.append(f"Há {total} {label} registrado(s); esse dado é distinto do texto documental.")
    if not chunks:
        chunks.append("Não há fatos de projeto suficientes no contexto autorizado desta página.")
    return " ".join(chunks), [
        {"source_type": "project_state", "label": "Registros autorizados do projeto"},
        {"source_type": "memo", "label": "Memos são registros analíticos, não texto do documento"},
        {"source_type": "coding", "label": "Códigos e codificações são registros distintos do corpus"},
    ]


def _page_answer(result: Mapping[str, Any], operation: str) -> tuple[str, list[dict[str, str]]]:
    pages = result.get("pages") if isinstance(result.get("pages"), list) else []
    if result.get("status") != "ok" or not pages:
        return _limited_answer(result)
    page = _mapping(pages[0])
    sentences = _sentences(page.get("text"))
    name = _compact(result.get("document_name"), 180) or "documento atual"
    number = page.get("page_number")
    if not sentences:
        return (
            "A página atual não contém uma unidade textual completa suficiente para uma resposta fundamentada. "
            "Há apenas título, fragmento ou texto insuficiente; abra uma página com parágrafos legíveis para analisá-la.",
            [{"source_type": "computed_result", "label": _source_label(name, number)}],
        )
    page_result = {"evidence": [{"document_name": name, "page_number": number, "text": sentence}
                                  for sentence in sentences[:MAX_NATIVE_EVIDENCE_ITEMS]], "retrieval": {"methods": ["current_page"]}}
    findings = EvidenceAnalyzer().analyze(page_result, operation)
    answer = NativeAnswerBuilder().from_findings(findings)
    return answer, [{"source_type": "document", "label": _source_label(name, number)}]


def _summary_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    if result.get("status") != "ok":
        return _limited_answer(result)
    summary = _compact(result.get("summary"), 5_000)
    if not summary:
        return _limited_answer(result)
    name = _compact(result.get("document_name"), 180)
    coverage = f"Cobertura: {result.get('pages_processed')} de {result.get('pages_total')} páginas."
    header = f"Síntese estrutural de {name}." if name else "Síntese estrutural do corpus."
    trace = [{"source_type": "document", "label": _compact(item.get("document_name"), 180) or "Documento"}
             for item in result.get("source_pages", [])[:MAX_NATIVE_EVIDENCE_ITEMS]
             if isinstance(item, Mapping)]
    return header + " " + coverage + "\n\n" + summary, trace or [{"source_type": "computed_result", "label": "Síntese extrativa"}]


def _corpus_count_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    if result.get("status") != "ok":
        return _limited_answer(result)
    documents, pages = result.get("documents_total"), result.get("pages_total")
    if not isinstance(documents, int) or not isinstance(pages, int):
        return _limited_answer(result)
    return (
        f"A Base autorizada tem {documents} documento(s) e {pages} página(s) com cobertura registrada.",
        [{"source_type": "computed_result", "label": "Contagem do corpus autorizado"}],
    )


def _limited_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    limitations = result.get("limitations") if isinstance(result.get("limitations"), list) else []
    detail = _compact(limitations[0], 500) if limitations else "Não houve evidência autorizada suficiente para responder."
    return detail, [{"source_type": "computed_result", "label": "Resultado limitado da recuperação"}]


class NativeCorpusReasoner:
    """Aplica operações fechadas aos achados estruturados autorizados."""

    def reason(self, response: object, tool_outputs: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
        plan = _mapping(response).get("native_plan")
        operation = str(_mapping(plan).get("operation") or "LOCATE")
        resolved = EvidenceRetriever.from_tool_outputs(response, tool_outputs)
        if not resolved:
            return ("A Análysis IA não recebeu um resultado autorizado para fundamentar a resposta.",
                    [{"source_type": "native_inference", "label": "Sem fonte disponível"}])
        name, result = resolved[0]
        if name == "search_corpus":
            if result.get("status") != "ok":
                return _limited_answer(result)
            findings = EvidenceAnalyzer().analyze(result, operation)
            answer = NativeAnswerBuilder().from_findings(findings)
            trace = [
                {"source_type": "document", "label": _source_label(entry.get("document_name"), entry.get("page_number"))}
                for entry in findings.evidence
            ]
            return answer, trace or [{"source_type": "computed_result", "label": "Resultado limitado da recuperação"}]
        if name in {"read_current_page", "read_document_pages"}:
            return _page_answer(result, operation)
        if name in {"summarize_document", "summarize_corpus"}:
            return _summary_answer(result)
        if name == "inspect_corpus":
            return _corpus_count_answer(result)
        if name == "get_platform_help":
            return _platform_answer(result)
        if name == "get_current_ui_context":
            return _ui_answer(result)
        if name == "get_project_context":
            return _project_answer(result)
        return _limited_answer(result)


class NativeAnswerBuilder:
    """Entrega linguagem humana e traços de proveniência, nunca JSON bruto."""

    @staticmethod
    def _quote(finding: StructuredFinding) -> str:
        return f"“{_compact(finding.text, 520)}”"

    @staticmethod
    def _source(finding: StructuredFinding) -> str:
        return "Fonte: " + _source_label(finding.document_name, finding.page_number) + "."

    @staticmethod
    def _definition_synthesis(finding: StructuredFinding) -> str:
        match = re.search(
            r"^(.{1,100}?)\s+(?:é|foi)\s+(?:aqui\s+)?(?:compreendid[oa]|definid[oa]|concebid[oa])\s+como\s+(.+?)[.!?]?$",
            finding.text, re.I,
        )
        if match:
            subject = _compact(match.group(1), 100)
            predicate = _compact(match.group(2), 260)
            return f"O trecho apresenta {subject} como {predicate}."
        return "O trecho contém uma formulação definicional explícita."

    def from_findings(self, findings: StructuredFindings) -> str:
        """Sintetiza primeiro, mostra evidência e conserva a fonte logo depois."""
        evidence = findings.evidence
        if not evidence:
            return "Não encontrei evidência documental suficiente para uma resposta fundamentada."
        categorized: dict[str, list[StructuredFinding]] = {}
        for finding in findings.findings:
            categorized.setdefault(finding.kind, []).append(finding)
        first = next(iter(evidence))
        has_complete_text = any(_sentences(entry.get("text") or entry.get("preview")) for entry in evidence)
        if not has_complete_text:
            return (
                "As evidências recuperadas contêm apenas títulos ou fragmentos; não há texto suficiente "
                "para sustentar uma resposta documental."
            )
        fallback = StructuredFinding(
            "excerpt", _compact(first.get("text") or first.get("preview"), 520),
            _compact(first.get("document_name"), 180) or "Documento",
            first.get("page_number") if isinstance(first.get("page_number"), int) else None,
        )
        operation = findings.operation

        if operation == "EXTRACT_DEFINITIONS":
            selected = (categorized.get("definition") or [fallback])[0]
            lines = [self._definition_synthesis(selected), "Evidência: " + self._quote(selected), self._source(selected)]
        elif operation == "CHARACTERIZE":
            selected = (categorized.get("characteristic") or [fallback])[0]
            lines = ["O trecho recuperado explicita uma característica do tema.",
                     "Evidência: " + self._quote(selected), self._source(selected)]
        elif operation == "RELATE":
            selected = (categorized.get("relation") or [fallback])[0]
            lines = ["A relação indicada abaixo é a que aparece explicitamente no trecho; ela não prova causalidade.",
                     "Evidência: " + self._quote(selected), self._source(selected)]
        elif operation == "ARGUMENT":
            selected = (categorized.get("argument") or [fallback])[0]
            lines = ["O argumento que pode ser sustentado é o formulado no trecho recuperado.",
                     "Evidência: " + self._quote(selected), self._source(selected)]
        elif operation == "EXEMPLIFY":
            selected = (categorized.get("example") or [fallback])[0]
            lines = ["O exemplo disponível na evidência recuperada é:", self._quote(selected), self._source(selected)]
        elif operation == "COMPARE":
            labels = EvidenceAggregator.document_page_labels(evidence)
            documents = {label.split(", página ", 1)[0] for label in labels}
            if len(documents) < 2:
                lines = ["Não há evidência recuperada em mais de um documento para sustentar uma comparação.",
                         "Evidência disponível: " + self._quote(fallback), self._source(fallback)]
            else:
                lines = ["Há evidências em mais de um documento, mas os trechos recuperados não permitem afirmar convergência ou divergência sem comparação interpretativa adicional."]
                for entry in evidence[:2]:
                    item = StructuredFinding("excerpt", _compact(entry.get("text") or entry.get("preview"), 420),
                                             _compact(entry.get("document_name"), 180) or "Documento",
                                             entry.get("page_number") if isinstance(entry.get("page_number"), int) else None)
                    lines.extend(("Evidência: " + self._quote(item), self._source(item)))
        elif operation == "DISTRIBUTE":
            counts: dict[str, int] = {}
            for entry in evidence:
                label = _compact(entry.get("document_name"), 180) or "Documento"
                counts[label] = counts.get(label, 0) + 1
            distribution = "; ".join(f"{name}: {count} trecho(s) recuperado(s)" for name, count in counts.items())
            lines = ["A distribuição abaixo descreve apenas as evidências recuperadas, não relevância analítica.",
                     distribution + ".", "Evidência: " + self._quote(fallback), self._source(fallback)]
        elif operation == "COOCCURRENCE":
            lines = ["A recuperação mostra proximidade nos trechos listados, mas não estabelece coocorrência analítica nem causalidade.",
                     "Evidência: " + self._quote(fallback), self._source(fallback)]
        elif operation == "LOCATE":
            lines = ["Foram localizadas evidências documentais para o tema solicitado."]
            for entry in evidence[:3]:
                item = StructuredFinding("excerpt", _compact(entry.get("text") or entry.get("preview"), 420),
                                         _compact(entry.get("document_name"), 180) or "Documento",
                                         entry.get("page_number") if isinstance(entry.get("page_number"), int) else None)
                lines.extend(("Evidência: " + self._quote(item), self._source(item)))
        else:
            lines = ["O texto disponível sustenta a seguinte leitura limitada ao trecho recuperado:",
                     "Evidência: " + self._quote(fallback), self._source(fallback)]

        if findings.limitations:
            lines.append("Limitação: " + findings.limitations[0])
        return "\n\n".join(line for line in lines if line)

    def build(self, response: object, tool_outputs: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
        return NativeCorpusReasoner().reason(response, tool_outputs)


class NativeCorpusEngine:
    """Orquestra a cadeia local: plano → recuperação → agregação → resposta.

    A recuperação real continua no executor read-only compartilhado; portanto
    este objeto não aceita IDs, caminhos nem texto documental do navegador.
    """

    def answer(self, response: object, tool_outputs: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
        return NativeAnswerBuilder().build(response, tool_outputs)


class AnalysisNativeProvider(AIProvider):
    """Adapter local: não importa SDKs nem recebe/chama credenciais."""

    provider_id = "analysis_native"

    def __init__(self, *, model: str | None = None):
        self.model = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_ANALYSIS_NATIVE_MODEL

    def generate(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        tool_mode: str = "auto",
        allowed_tool_names: Sequence[str] | None = None,
    ) -> Any:
        del instructions, tools, tool_mode
        question = _question_from_input(input_items)
        allowed = tuple(name for name in (allowed_tool_names or ()) if isinstance(name, str) and name)
        plan = native_question_plan(question, allowed_tool_names=allowed)
        if plan.tool_name is None:
            raise AIProviderError(
                "A Análysis IA ainda não possui uma fonte interna autorizada para responder a essa pergunta.",
                status_code=422,
                reason_class="unsupported_capability",
            )
        return _response_with_call(plan, question)

    def continue_with_tool_outputs(
        self,
        *,
        instructions: str,
        input_items: Sequence[Any],
        response: Any,
        tool_outputs: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Any:
        del instructions, input_items, tools
        answer, trace = NativeCorpusEngine().answer(response, tool_outputs)
        return {"output": [], "output_text": answer, "native_trace": trace}

    def test_connection(self) -> None:
        """Teste local de inicialização: não faz rede e valida dependências base."""
        try:
            from .platform_help import load_help_registry
            # Imports explícitos verificam que as camadas reutilizadas pelo
            # executor estão presentes, sem ler corpus nem iniciar provider
            # remoto. Os dados continuam sujeitos à autorização por turno.
            from . import assistant_document_retrieval, qualitative_corpus
            del assistant_document_retrieval, qualitative_corpus
            load_help_registry()
        except Exception as error:
            raise AIProviderError(
                "A base de conhecimento local da Análysis IA não está disponível.",
                reason_class="native_unavailable",
            ) from error


__all__ = [
    "AnalysisNativeProvider", "DEFAULT_ANALYSIS_NATIVE_MODEL", "EvidenceAggregator", "EvidenceAnalyzer",
    "EvidenceRanker", "EvidenceRetriever", "MAX_NATIVE_EVIDENCE_ITEMS", "NativeAnswerBuilder",
    "NativeCorpusEngine", "NativeCorpusReasoner", "StructuredFinding", "StructuredFindings",
]
