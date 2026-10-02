from __future__ import annotations

import unittest
from pathlib import Path
import shutil
import subprocess

from analyzer.dashboard_metrics import frequencia_relativa_por_documento
from platform_core.analyses import systematic_chart_data


class TermAnalysisChartDataTests(unittest.TestCase):
    def test_relative_frequency_uses_existing_extracted_word_counts_without_truncating_documents(self):
        data = frequencia_relativa_por_documento(
            ["Documento A", "Documento B", "Documento C"],
            [[2, 1, 0], [3, 4, 5]],
            [
                {"arquivo": "Documento A.pdf", "palavras_analisadas": 1_000},
                {"arquivo": "Documento B.pdf", "palavras_analisadas": 2_500},
                {"arquivo": "Documento C.pdf", "palavras_analisadas": 0},
            ],
        )

        self.assertEqual(data["documentos"], ["Documento A", "Documento B", "Documento C"])
        self.assertEqual(data["ocorrencias"], [5, 5, 5])
        self.assertEqual(data["palavras"], [1_000, 2_500, 0])
        self.assertEqual(data["por_mil"], [5.0, 2.0, None])

    def test_result_template_exposes_only_the_two_requested_term_charts(self):
        template = (Path(__file__).resolve().parents[1] / "templates/resultado.html").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static/js/dashboard.js").read_text(encoding="utf-8")
        self.assertEqual(template.count('id="grafico-termos-documentos"'), 2)
        self.assertEqual(template.count('id="grafico-frequencia-relativa"'), 2)
        self.assertNotIn('id="grafico-termos-frequencia"', template)
        self.assertNotIn('id="grafico-ocorrencias-pagina"', template)
        self.assertIn('class="analysis-chart-scroll"', template)
        self.assertIn("type: 'heatmap'", script)
        self.assertIn("por 1.000 palavras", script)
        self.assertNotIn(".slice(", script)

    def test_term_chart_frontend_renders_all_matrix_rows_and_relative_frequencies(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (Path(__file__).resolve().parents[1] / "static/js/dashboard.js").read_text(encoding="utf-8")
        script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(id) { this.id=id; this.style={}; this.parentElement={clientWidth:300}; this.children=[]; this.classList={contains:()=>false,add(){},remove(){}}; }
  replaceChildren(){ this.children=[]; } appendChild(child){ this.children.push(child); }
}
const heatmap=new Element('grafico-termos-documentos'), relative=new Element('grafico-frequencia-relativa');
const data={indicadores:{},termos_documentos:{termos:['raça','classe'],documentos:['Documento A','Documento B','Documento C'],matriz:[[2,1,0],[3,4,5]]},frequencia_relativa:{documentos:['Documento A','Documento B','Documento C'],ocorrencias:[5,5,5],palavras:[1000,2500,0],por_mil:[5,2,null]}};
const document={documentElement:{dataset:{theme:'dark'}},getElementById:id=>id==='dados-dashboard'?{textContent:JSON.stringify(data)}:id===heatmap.id?heatmap:id===relative.id?relative:null,createElement:()=>new Element('notice'),querySelectorAll:()=>[],addEventListener(){}};
const calls=[]; const Plotly={react:(...args)=>calls.push(args),purge(){},Plots:{resize(){}}};
const window={Plotly,PesquisaPdfPlotTheme:{palette:()=>['one','two'],color:()=> '#fff',layout:margin=>({margin}),axis:options=>options}};
""" + source + r"""
assert.equal(calls.length,2);
assert.equal(calls[0][1][0].type,'heatmap'); assert.deepEqual(calls[0][1][0].z,[[2,1,0],[3,4,5]]);
assert.deepEqual(calls[1][1][0].x,[5,2,0]); assert.equal(calls[1][1][0].customdata[2][2],'não disponível');
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


class StructuredAnalysisChartDataTests(unittest.TestCase):
    def test_group_document_heatmap_and_internal_composition_include_all_persisted_items(self):
        data = systematic_chart_data({
            "grupos": {"politica": "Política", "educacao": "Educação"},
            "documentos": [{"arquivo_pdf": "A.pdf"}, {"arquivo_pdf": "B.pdf"}],
            "ocorrencias": [
                {"arquivo_pdf": "A.pdf", "entidade_canonica": "Escola", "grupo": ["educacao"]},
                {"arquivo_pdf": "A.pdf", "entidade_canonica": "Lei", "grupo": ["politica"]},
                {"arquivo_pdf": "B.pdf", "entidade_canonica": "Lei", "grupo": ["politica", "educacao"]},
            ],
        })

        self.assertEqual(data["grupos_documentos"], {
            "grupos": ["Política", "Educação"],
            "documentos": ["A.pdf", "B.pdf"],
            "matriz": [[1, 1], [1, 1]],
        })
        composition = data["composicao_grupos"]
        self.assertEqual(composition["entidades"], ["Lei", "Escola"])
        self.assertEqual(composition["contagens"], [[2, 1], [0, 1]])
        self.assertEqual(composition["percentuais"], [[100.0, 50.0], [0.0, 50.0]])

    def test_structured_template_has_no_first_occurrences_interface(self):
        template = (Path(__file__).resolve().parents[1] / "templates/platform/analysis_dashboard.html").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static/js/structured_analysis_dashboard.js").read_text(encoding="utf-8")
        self.assertIn('id="grafico-grupos-documentos"', template)
        self.assertIn('id="grafico-composicao-grupos"', template)
        self.assertEqual(template.count('class="col-12"'), 2)
        self.assertNotIn('col-xl-7', template)
        self.assertNotIn('col-xl-5', template)
        self.assertNotIn("Primeiras ocorrências", template)
        self.assertIn('class="analysis-chart-scroll"', template)
        self.assertIn("type: 'heatmap'", script)
        self.assertIn("barmode: 'stack'", script)
        self.assertNotIn(".slice(", script)

    def test_structured_chart_frontend_keeps_every_group_document_and_entity(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (Path(__file__).resolve().parents[1] / "static/js/structured_analysis_dashboard.js").read_text(encoding="utf-8")
        script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(id) { this.id=id; this.style={}; this.parentElement={clientWidth:300}; this.children=[]; this.classList={contains:()=>false,add(){},remove(){}}; }
  replaceChildren(){ this.children=[]; } appendChild(child){ this.children.push(child); }
}
const heatmap=new Element('grafico-grupos-documentos'), composition=new Element('grafico-composicao-grupos');
const data={grupos_documentos:{grupos:['Política','Educação'],documentos:['A.pdf','B.pdf'],matriz:[[1,1],[1,1]]},composicao_grupos:{grupos:['Política','Educação'],entidades:['Lei','Escola'],contagens:[[2,1],[0,1]],percentuais:[[100,50],[0,50]]}};
const document={documentElement:{dataset:{theme:'light'}},getElementById:id=>id==='structured-analysis-chart-data'?{textContent:JSON.stringify(data)}:id===heatmap.id?heatmap:id===composition.id?composition:null,createElement:()=>new Element('notice'),querySelectorAll:()=>[],addEventListener(){}};
const calls=[]; const Plotly={react:(...args)=>calls.push(args),purge(){},Plots:{resize(){}}};
const window={Plotly,PesquisaPdfPlotTheme:{palette:()=>['one','two'],color:()=> '#111',layout:margin=>({margin}),axis:options=>options,legend:options=>options}};
""" + source + r"""
assert.equal(calls.length,2);
assert.equal(calls[0][1][0].type,'heatmap'); assert.deepEqual(calls[0][1][0].z,[[1,1],[1,1]]);
assert.equal(calls[1][1].length,2); assert.deepEqual(calls[1][1][0].x,[100,50]); assert.equal(calls[1][2].barmode,'stack');
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
