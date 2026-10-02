"""Contrato do manual versionado e sua integração limitada ao Assistente."""

from pathlib import Path
import shutil
import subprocess
import unittest

from analyzer import v1, v3
from historico_racial import exporter as structured_exporter
from platform_core.assistant_ai_tools import AssistantToolExecutor
from platform_core.assistant_context import ASSISTANT_CONTEXTS
from platform_core.platform_help import (
    MAX_ASSISTANT_SECTION_CHARS,
    MAX_ASSISTANT_SECTIONS,
    assistant_help,
    load_help_registry,
    tutorial_for,
)
from platform_helpers import create_user, isolated_platform, login


ROOT = Path(__file__).resolve().parents[1]


class PlatformHelpRegistryTests(unittest.TestCase):
    @staticmethod
    def _section_text(section):
        return " ".join([
            section["title"],
            *section.get("keywords", []),
            *section["body"],
            *(item["name"] for item in section.get("items", [])),
            *(item["description"] for item in section.get("items", [])),
        ])

    def test_registry_is_versioned_and_covers_every_assistant_context(self):
        registry = load_help_registry()
        self.assertRegex(registry["version"], r"^\d{4}\.\d{2}\.\d{2}$")
        self.assertTrue(set(ASSISTANT_CONTEXTS).issubset(registry["tutorials"]))
        for key in ASSISTANT_CONTEXTS:
            with self.subTest(context=key):
                tutorial = tutorial_for(key)
                self.assertEqual(tutorial["key"], key)
                self.assertGreaterEqual(len(tutorial["sections"]), 3)

    def test_registry_documents_real_options_exports_and_qualitative_controls(self):
        source = (ROOT / "resources" / "platform_tutorials.json").read_text(encoding="utf-8")
        for term in (
            "Regex", "Lexical", "Semântica", "Limiar", "Diagnostico OCR",
            "DOCUMENTOS", "OCORRENCIAS", "CODIFICACAO", "COOCORRENCIAS",
            "Margem analítica", "Código in vivo", "Memo contextual", "Modo foco",
            "Resultados", "Resumo lexical", "Codificação",
        ):
            with self.subTest(term=term):
                self.assertIn(term, source)

    def test_documented_export_names_and_columns_follow_the_actual_exporters(self):
        term_sections = {section["id"]: section for section in tutorial_for("term_search")["sections"]}
        structured_sections = {section["id"]: section for section in tutorial_for("structured_search")["sections"]}
        qualitative_sections = {section["id"]: section for section in tutorial_for("qualitative")["sections"]}
        lexical_guide = self._section_text(term_sections["planilha-lexical"])
        hybrid_guide = self._section_text(term_sections["planilha-hibrida"])
        structured_guide = " ".join(self._section_text(structured_sections[section_id]) for section_id in (
            "planilha-documentos", "planilha-ocorrencias", "planilha-codificacao", "planilha-coocorrencias",
        ))
        qualitative_guide = self._section_text(qualitative_sections["relatorio"])
        for sheet in ("Ocorrencias", "Resumo por termo", "Resumo por tipo", "Termos pesquisados", "Diagnostico OCR"):
            self.assertIn(sheet, lexical_guide)
        for sheet in ("Resultados", "Resumo lexical", "Resumo semântico", "Resumo por consulta"):
            self.assertIn(sheet, hybrid_guide)
        for sheet in ("DOCUMENTOS", "OCORRENCIAS", "CODIFICACAO", "COOCORRENCIAS"):
            self.assertIn(sheet, structured_guide)
        for column in ("Validação manual", "Observações do pesquisador", "Similaridade semântica"):
            self.assertIn(column, lexical_guide + hybrid_guide + structured_guide)
            self.assertTrue(column in v1.salvar_excel_completo.__code__.co_consts or column in v3.COLUNAS_RESULTADOS)
        self.assertEqual(structured_exporter.CODING_HEADERS[0], "ID ocorrência")
        for column in ("Código", "Documento", "Página", "Trecho codificado", "Memo contextual"):
            self.assertIn(column, qualitative_guide)

        for header in (
            *structured_exporter.DOCUMENT_HEADERS,
            *structured_exporter.OCCURRENCE_HEADERS,
            *structured_exporter.CODING_HEADERS,
            *structured_exporter.COOCCURRENCE_HEADERS,
        ):
            with self.subTest(structured_header=header):
                self.assertIn(header, structured_guide)

    def test_structured_tutorial_covers_real_workflow_controls_and_limits(self):
        tutorial = tutorial_for("structured_search")
        by_id = {section["id"]: section for section in tutorial["sections"]}
        expected_sections = {
            "modelo-mental", "fluxo-operacional", "grupos", "entidades-e-variantes",
            "opcoes-da-tela", "localizacao-lexical", "metodo-hibrido", "ocr",
            "ocorrencias-e-contexto", "tela-de-resultados", "planilha-documentos",
            "planilha-ocorrencias", "planilha-codificacao", "planilha-coocorrencias",
            "leitura-analitica", "exemplo-completo", "problemas-e-limites", "quando-usar-e-comparar",
            "execucao-e-progresso", "exemplo-geografico-e-glossario",
        }
        self.assertTrue(expected_sections.issubset(by_id))
        content = " ".join(
            paragraph for section in tutorial["sections"] for paragraph in section["body"]
        )
        for fact in (
            "Grupo → Entidade → Variante ativa", "0,50 a 0,90", "limites de palavra",
            "OCR é acionado automaticamente", "Validação pendente", "não preenche pares automaticamente",
            "não possui integração automática que envie ocorrências para Quali-dados",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, content)

        index = (ROOT / "templates" / "historico_racial" / "index.html").read_text(encoding="utf-8")
        for control in ("Método de raspagem", "Lexical", "Híbrido", "Limiar de similaridade", "Arquivos PDF"):
            with self.subTest(control=control):
                self.assertIn(control, index)
                self.assertIn(control, content)

    def test_main_tool_tutorials_have_operational_chapters_and_indexed_controls(self):
        term = tutorial_for("term_search")
        structured = tutorial_for("structured_search")
        qualitative = tutorial_for("qualitative")
        expected = {
            "term_search": {
                "modelo-mental", "quando-usar-e-comparar", "base-e-documentos", "termos-e-entrada", "metodo-lexical",
                "metodo-hibrido", "ocr-e-extracao", "execucao-e-progresso", "tela-de-resultados",
                "ocorrencias-e-validacao", "planilha-lexical", "planilha-hibrida", "analise-da-planilha",
                "exemplo-completo", "problemas-limites-e-glossario",
            },
            "structured_search": {
                "modelo-mental", "fluxo-operacional", "grupos", "entidades-e-variantes",
                "opcoes-da-tela", "localizacao-lexical", "metodo-hibrido", "ocr",
                "ocorrencias-e-contexto", "tela-de-resultados", "planilha-documentos",
                "planilha-ocorrencias", "planilha-codificacao", "planilha-coocorrencias",
                "leitura-analitica", "exemplo-completo", "problemas-e-limites",
                "quando-usar-e-comparar", "execucao-e-progresso", "exemplo-geografico-e-glossario",
            },
            "qualitative": {
                "visao-geral", "quando-usar-e-comparar", "passos", "explorador", "codificacao", "leitor-e-margem",
                "busca-e-autocodificacao", "busca-literal-e-regex", "busca-lexical", "busca-semantica",
                "relatorio", "assistente", "leitura",
                "texto-canonico-e-ocr", "codigos-e-cores", "autocodificacao-detalhada",
            },
        }
        tutorials = {item["key"]: item for item in (term, structured, qualitative)}
        for key, required_sections in expected.items():
            with self.subTest(tutorial=key):
                by_id = {section["id"]: section for section in tutorials[key]["sections"]}
                self.assertTrue(required_sections.issubset(by_id))
                for section_id in required_sections:
                    self.assertGreaterEqual(len(by_id[section_id]["body"]), 2)

        # Aliases/keywords são taxonomia interna para recuperação: não são uma
        # lista exibida no modal, mas evitam depender só da redação literal da
        # pergunta feita pela pessoa usuária.
        for tutorial in (term, qualitative):
            for section in tutorial["sections"]:
                with self.subTest(tutorial=tutorial["key"], section=section["id"]):
                    self.assertTrue(section.get("keywords"))

    def test_documented_controls_map_to_their_real_tool_templates(self):
        term = {section["id"]: section for section in tutorial_for("term_search")["sections"]}
        structured = {section["id"]: section for section in tutorial_for("structured_search")["sections"]}
        qualitative = {section["id"]: section for section in tutorial_for("qualitative")["sections"]}
        term_template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        structured_template = (ROOT / "templates" / "historico_racial" / "index.html").read_text(encoding="utf-8")
        qualitative_template = (ROOT / "templates" / "platform" / "qualitative_reader.html").read_text(encoding="utf-8")

        mappings = (
            (term_template, "Arquivos PDF", term["base-e-documentos"]),
            (term_template, "Termos para pesquisar", term["termos-e-entrada"]),
            (term_template, "Lexical", term["metodo-lexical"]),
            (term_template, "Híbrido", term["metodo-hibrido"]),
            (term_template, "Limiar de similaridade", term["metodo-hibrido"]),
            (structured_template, "Método de raspagem", structured["opcoes-da-tela"]),
            (structured_template, "Lexical", structured["localizacao-lexical"]),
            (structured_template, "Híbrido", structured["metodo-hibrido"]),
            (structured_template, "Limiar de similaridade", structured["opcoes-da-tela"]),
            (qualitative_template, "Regex", qualitative["busca-e-autocodificacao"]),
            (qualitative_template, "Maiúsculas e minúsculas", qualitative["busca-e-autocodificacao"]),
            (qualitative_template, "Autocodificação", qualitative["busca-e-autocodificacao"]),
            (qualitative_template, "Múltiplos termos", qualitative["busca-e-autocodificacao"]),
            (qualitative_template, "Rejeição contextual", qualitative["busca-e-autocodificacao"]),
            (qualitative_template, "Aplicar código", qualitative["codificacao"]),
            (qualitative_template, "Código in vivo", qualitative["codificacao"]),
            (qualitative_template, "Criar memo", qualitative["codificacao"]),
            (qualitative_template, "Modo foco", qualitative["leitor-e-margem"]),
        )
        for template, control, section in mappings:
            with self.subTest(control=control, chapter=section["id"]):
                self.assertIn(control, template)
                self.assertIn(control, self._section_text(section))

        search_chapter = {section["id"]: section for section in tutorial_for("qualitative_search")["sections"]}["opcoes"]
        self.assertIn("Todos os documentos", self._section_text(search_chapter))

    def test_reader_keeps_detailed_search_options_in_the_central_tutorial(self):
        reader = tutorial_for("qualitative_reader")
        self.assertIn("opcoes", {section["id"] for section in reader["sections"]})
        template = (ROOT / "templates" / "platform" / "qualitative_reader.html").read_text(encoding="utf-8")
        self.assertNotIn("help_button(", template)
        self.assertNotIn("_qualitative_search_help", template)

    def test_assistant_retrieval_is_bounded_and_can_find_another_tool_section(self):
        help_data = assistant_help("Qual a diferença entre Regex, Lexical e Semântica?", "qualitative_reader")
        self.assertTrue(help_data["official_platform_documentation"])
        self.assertLessEqual(len(help_data["sections"]), MAX_ASSISTANT_SECTIONS)
        self.assertTrue(any(section["tutorial_key"] == "qualitative_search" for section in help_data["sections"]))
        self.assertTrue(all(len(section["content"]) <= MAX_ASSISTANT_SECTION_CHARS for section in help_data["sections"]))

    def test_assistant_retrieves_relevant_structured_chapters_without_sending_the_manual(self):
        cases = {
            "Qual a diferença entre grupo e entidade?": {"grupos", "entidades-e-variantes"},
            "Como é feita a localização lexical e o OCR?": {"localizacao-lexical", "ocr"},
            "Como analiso a planilha CODIFICACAO?": {"planilha-codificacao", "leitura-analitica"},
            "Nenhuma ocorrência foi encontrada; o que verificar?": {"problemas-e-limites"},
        }
        for question, expected_ids in cases.items():
            with self.subTest(question=question):
                result = assistant_help(question, "structured_search")
                received = {section["id"] for section in result["sections"]}
                self.assertTrue(received & expected_ids)
                self.assertLessEqual(len(result["sections"]), MAX_ASSISTANT_SECTIONS)
                self.assertTrue(all(len(section["content"]) <= MAX_ASSISTANT_SECTION_CHARS
                                    for section in result["sections"]))

    def test_assistant_retrieves_granular_term_and_qualitative_chapters_from_local_index(self):
        cases = {
            ("term_search", "Como funciona o método Híbrido e seu limiar?"): {"metodo-hibrido"},
            ("term_search", "Como analiso a planilha?"): {
                "planilha-lexical", "planilha-hibrida", "analise-da-planilha"
            },
            ("qualitative", "Como faço uma codificação?"): {"codificacao"},
            ("qualitative", "Como funciona a margem analítica?"): {"leitor-e-margem"},
            ("qualitative", "Como uso autocodificação e rejeição contextual?"): {
                "busca-e-autocodificacao", "autocodificacao-detalhada"
            },
            ("qualitative_reader", "Como uso Regex no leitor?"): {"opcoes"},
        }
        for (context_key, question), expected_ids in cases.items():
            with self.subTest(context=context_key, question=question):
                result = assistant_help(question, context_key)
                received = {section["id"] for section in result["sections"]}
                self.assertTrue(received & expected_ids)

                self.assertLessEqual(len(result["sections"]), MAX_ASSISTANT_SECTIONS)


class PlatformHelpAssistantIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_platform_help_keeps_access_facts_and_adds_official_documentation(self):
        executor = AssistantToolExecutor(
            user=self.user, context_key="term_search", project_context=None, provider=object(),
        )
        result, sources = executor.execute("get_platform_help", {"topic": "exportação OCR"})
        self.assertEqual(sources, [])
        self.assertTrue(result["read_only"])
        self.assertIn("access", result)
        self.assertIn("official_documentation", result)
        official = result["official_documentation"]
        self.assertEqual(official["page"]["key"], "term_search")
        self.assertLessEqual(len(official["sections"]), MAX_ASSISTANT_SECTIONS)

    def test_platform_help_exposes_specific_structured_chapters_without_a_provider_call(self):
        executor = AssistantToolExecutor(
            user=self.user, context_key="structured_search", project_context=None, provider=object(),
        )
        result, sources = executor.execute("get_platform_help", {"topic": "Como analiso a planilha CODIFICACAO?"})
        self.assertEqual(sources, [])
        official = result["official_documentation"]
        self.assertEqual(official["page"]["key"], "structured_search")
        self.assertIn("planilha-codificacao", {section["id"] for section in official["sections"]})
        self.assertTrue(official["official_platform_documentation"])

    def test_info_is_visible_only_on_the_three_main_tool_entries(self):
        client = self.app.test_client()
        login(client, self.user.email)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('id="platform-tutorial-dialog"', html)
        self.assertNotIn("data-platform-tutorial-open", html)

        term_response = client.get("/raspagem-livre")
        self.assertEqual(term_response.status_code, 200)
        term_html = term_response.get_data(as_text=True)
        self.assertEqual(term_html.count('id="platform-tutorial-dialog"'), 1)
        self.assertEqual(term_html.count("data-platform-tutorial-open"), 1)
        self.assertIn('aria-label="Abrir tutorial da Busca por termos"', term_html)

        main_entries = {
            ROOT / "templates" / "index.html": "Abrir tutorial da Busca por termos",
            ROOT / "templates" / "historico_racial" / "index.html": "Abrir tutorial da Busca estruturada",
            ROOT / "templates" / "platform" / "_qualitative_page_intro.html": "Como funciona a Análise quali-dados",
        }
        for template_path, label in main_entries.items():
            with self.subTest(template=template_path.name):
                template = template_path.read_text(encoding="utf-8")
                self.assertIn("tutorial_button", template)
                self.assertIn(label, template)
                self.assertIn("tutorial_dialog", template)
                self.assertIn("js/platform_tutorial.js", template)

        for template_path in (
            ROOT / "templates" / "platform" / "base.html",
            ROOT / "templates" / "platform" / "qualitative_coding_report.html",
            ROOT / "templates" / "historico_racial" / "resultado.html",
        ):
            with self.subTest(no_info=template_path.name):
                self.assertNotIn("data-platform-tutorial-open", template_path.read_text(encoding="utf-8"))


class PlatformTutorialFrontendContractTests(unittest.TestCase):
    def test_component_has_sticky_header_toc_internal_scroll_and_keyboard_safe_controls(self):
        template = (ROOT / "templates" / "platform" / "_tutorial_info.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "platform_tutorial.js").read_text(encoding="utf-8")
        self.assertIn("<dialog", template)
        self.assertIn("data-platform-tutorial-close", template)
        self.assertIn("aria-controls=\"platform-tutorial-dialog\"", template)
        for rule in ("position: sticky", "overflow: auto", "platform-tutorial-dialog__toc", "@media (max-width: 640px)"):
            self.assertIn(rule, css)
        for token in ("showModal", "dialog.close", "focusSection", "platformTutorialSection"):
            self.assertIn(token, script)

    def test_dialog_opens_to_section_and_closes_without_affecting_other_controls(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (ROOT / "static" / "js" / "platform_tutorial.js").read_text(encoding="utf-8")
        harness = r"""
const assert = require('node:assert/strict');
const make = () => ({listeners:{}, addEventListener(name, fn) { this.listeners[name] = fn; }, emit(name, event={}) { this.listeners[name](event); }});
const close = make(), first = make(), section = {scrolled:false, focused:false, scrollIntoView() { this.scrolled=true; }, focus() { this.focused=true; }};
const dialog = make(); dialog.open=false; dialog.showModal=()=>{dialog.open=true}; dialog.close=()=>{dialog.open=false}; dialog.querySelector=(selector)=>selector === '[data-platform-tutorial-close]' ? close : selector === '#tutorial-opcoes' ? section : null;
first.dataset={platformTutorialSection:'opcoes'};
global.CSS={escape:value=>value};
global.document={querySelector:selector=>selector === '#platform-tutorial-dialog' ? dialog : null, querySelectorAll:selector=>selector === '[data-platform-tutorial-open]' ? [first] : []};
SOURCE
first.emit('click'); assert.equal(dialog.open,true); assert.equal(section.scrolled,true); assert.equal(section.focused,true);
close.emit('click'); assert.equal(dialog.open,false);
dialog.open=true; dialog.emit('click',{target:dialog}); assert.equal(dialog.open,false);
""".replace("SOURCE", "(() => {\n" + source + "\n})();")
        result = subprocess.run([node, "-e", harness], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
