"""Progresso real ponderado por páginas, publicação e apresentação compartilhada."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_helpers import create_user, isolated_platform, login
from platform_core.analyses import create_analysis, preserve_documents
from platform_core.extensions import db
from platform_core.models import Project
from platform_core.qualitative_corpus import prepare_qualitative_corpus
from platform_core.qualitative_routes import _progress, _progress_lock, _upload_lock
from platform_core.scraping_types import QUALITATIVE_TOOL
from test_qualitative_corpus import pdf_bytes

ROOT = Path(__file__).resolve().parents[1]


class QualitativeProgressTests(unittest.TestCase):
    def test_all_pages_weighted_across_documents_and_100_only_after_publication(self):
        with isolated_platform() as app, tempfile.TemporaryDirectory() as directory:
            user = create_user(role="admin")
            project = Project(owner_user_id=user.id, name="Progresso", scrape_type="qualitative")
            db.session.add(project); db.session.commit()
            analysis = create_analysis(user_id=user.id, project_id=project.id, tool_id=QUALITATIVE_TOOL,
                                       tool_version="manual-v1", name="Progresso", parameters={})
            sources = []
            for index, count in enumerate((2, 6)):
                path = Path(directory) / f"pdf{index}.pdf"; path.write_bytes(pdf_bytes(count))
                sources.append((path, path.name))
            preserve_documents(analysis, sources)
            client = app.test_client(); login(client)
            progress_url = f"/analise-qualitativa/bases/{analysis.id}/progresso"
            self.assertEqual(client.get(progress_url).json["percentual"], 0)
            events = []
            def report(event):
                events.append(event)
                with _progress_lock:
                    _progress.setdefault(analysis.id, {}).update(event)
            manifest = prepare_qualitative_corpus(analysis, report)
            values = [event["percentual"] for event in events if "percentual" in event]
            self.assertEqual(values, sorted(values))
            self.assertEqual(values[-1], 99)
            self.assertEqual(sum(len(doc["pages"]) for doc in manifest["documents"]), 8)
            for event in events:
                if event.get("pages_completed_in_document") == 2 and event.get("document_index") == 1:
                    self.assertEqual(event["percentual"], 25)  # não 50% por contar PDFs igualmente
            data = client.get(progress_url).json
            self.assertEqual((data["pages_completed"], data["total_pages"]), (8, 8))
            self.assertEqual((data["document_index"], data["document_count"]), (2, 2))
            self.assertEqual((data["page_number"], data["page_count"]), (6, 6))
            self.assertIsNone(data["result_url"])
            analysis.status = "concluida"; db.session.commit()
            lock = _upload_lock(analysis.id); lock.touch()
            # Acréscimo incremental: status antigo concluído não pode antecipar o redirect.
            data = client.get(progress_url).json
            self.assertEqual(data["status"], "processando"); self.assertEqual(data["percentual"], 99)
            lock.unlink()
            data = client.get(progress_url).json
            self.assertEqual(data["percentual"], 100); self.assertIsNotNone(data["result_url"])
            with _progress_lock:
                _progress.pop(analysis.id, None)

    def test_poll_updates_shared_bar_monotonically_details_and_completion(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        code = r"""
const assert=require('node:assert/strict'),window={};
const attrs={'aria-valuenow':'0'},classes=new Set();
const fill={style:{}}, title={},stage={},detail={},error={};
const bar={getAttribute:k=>attrs[k],setAttribute:(k,v)=>attrs[k]=v,removeAttribute:k=>delete attrs[k],
 classList:{toggle:(k,on)=>on?classes.add(k):classes.delete(k)},querySelector:()=>fill};
const panel={dataset:{progressUrl:'/progresso'},querySelector:s=>({'h2':title,'[data-progress-stage]':stage,
 '.loading-progress-detail':detail,'[data-progress-error]':error,'[role="progressbar"]':bar})[s]};
const document={querySelector:()=>panel},timers=[],setTimeout=(fn,ms)=>timers.push({fn,ms});
let data={},redirect=null;const location={assign:url=>redirect=url};
const fetch=async()=>({ok:true,json:async()=>data});
""" + (ROOT / "static/js/processing_progress.js").read_text(encoding="utf-8") + r"""
window.ProcessingProgress.updateBar(bar,fill,250);assert.equal(fill.style.width,'100%');
window.ProcessingProgress.updateBar(bar,fill,null);assert.equal(classes.has('is-indeterminate'),true);
attrs['aria-valuenow']='0';
data={percentual:21,status:'processando',stage:'Extraindo texto…',document_name:'Livro.pdf',
 page_number:49,page_count:238,document_index:1,document_count:2};
""" + (ROOT / "static/js/qualitative_progress.js").read_text(encoding="utf-8") + r"""
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(fill.style.width,'21%');assert.equal(attrs['aria-valuenow'],'21');
 assert.ok(title.textContent.includes('21%'));assert.equal(classes.has('is-indeterminate'),false);
 assert.equal(detail.textContent,'Livro.pdf · página 49 de 238 (documento 1 de 2)');
 assert.equal(stage.textContent,'Extraindo texto…');
 for(const percent of [46,20,76,99]) {
   data.percentual=percent;await timers.shift().fn();
   assert.equal(Number(attrs['aria-valuenow']),Math.max(46,percent));
 }
 data={percentual:100,status:'concluida',result_url:'/reader'};await timers.shift().fn();
 assert.equal(fill.style.width,'100%');assert.ok(title.textContent.includes('100%'));
 assert.equal(redirect,null);assert.equal(timers[0].ms,400);timers.shift().fn();assert.equal(redirect,'/reader');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        for template in ("templates/index.html", "templates/historico_racial/index.html", "templates/platform/qualitative_pending.html"):
            self.assertIn("js/processing_progress.js", (ROOT / template).read_text(encoding="utf-8"))
