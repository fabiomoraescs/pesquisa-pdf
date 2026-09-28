"""Exercita as funções reais da text layer com DOM mínimo, sem PDF/OCR."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")
DOM = r"""
const assert = require('node:assert/strict');
class Element {
  constructor() { this.style = {}; this.dataset = {}; this.children = []; }
  setAttribute() {}
  append(child) { child.parent = this; this.children.push(child); }
  replaceChildren(...children) { this.children = []; children.forEach(child => this.append(child)); }
  querySelector(selector) { return this.children.find(child => selector === '.' + child.className) || null; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  get childElementCount() { return this.children.length; }
}
const document = { createElement(tag) {
  const element = new Element();
  if (tag === 'canvas') element.getContext = () => ({measureText: text => ({width: text.length * 10})});
  return element;
}};
const getComputedStyle = () => ({fontFamily: 'sans-serif'});
const node = { surface: new Element(), shell: new Element(), rendered: 1 };
node.surface.clientWidth = 200;
node.shell.dataset.pageNumber = '12';
const layout = {layout_available: true, width: 200, height: 300, items: [
  {start: 0, end: 1, text: 'I', bbox: [10, 10, 11, 30]},
  {start: 2, end: 3, text: 'W', bbox: [20, 10, 80, 30]},
  {start: 4, end: 5, text: 'R', bbox: [90, 10, 110, 20], quad: [[110,10],[110,20],[90,10],[90,20]]}
]};
"""


class QualitativeViewerFrontendTests(unittest.TestCase):
    def test_selection_accepts_text_and_element_boundaries_with_unicode_offsets(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        code = SOURCE[SOURCE.index("  const selectionBoundary ="):SOURCE.index("  const selectionAnchor =")]
        checks = r"""
const assert = require('node:assert/strict');
const Node = {TEXT_NODE:3, ELEMENT_NODE:1};
const item = {nodeType:1, textContent:'A😀ç', matches:s => s === '.platform-qualitative-text-item'};
const text = {nodeType:3, textContent:item.textContent, parentElement:{closest:() => item}};
const layer = {nodeType:1, childNodes:[item], matches:s => s === '.platform-qualitative-text-layer'};
assert.equal(selectionBoundary(text, 3, true).offset, 2);
assert.equal(selectionBoundary(item, 0, true).offset, 0);
assert.equal(selectionBoundary(item, 1, true).offset, 3);
assert.equal(selectionBoundary(layer, 0, false).offset, 0);
assert.equal(selectionBoundary(layer, 1, true).offset, 3);
assert.equal(selectionBoundary({nodeType:1,matches:() => false}, 0, true), null);
"""
        result = subprocess.run([node, "-e", code + checks], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_js(self, checks, *, decorate=False):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        code = SOURCE[SOURCE.index("  const placeBox ="):SOURCE.index("  const renderExcerptOverlay =")]
        if decorate:
            code += SOURCE[SOURCE.index("  const decoratePage ="):SOURCE.index("  const showResult =")]
        result = subprocess.run([node, "-e", DOM + code + checks], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_narrow_wide_and_rotated_glyphs_survive_rerender_and_zoom(self):
        self.run_js(r"""
assert.equal(renderTextLayer(node, layout), 3);
assert.equal(node.surface.childElementCount, 1);
let layer = node.surface.children[0];
assert.equal(layer.children[0].style.transform, 'matrix(0.1,0,0,1,0,0)');
assert.equal(layer.children[2].style.transform, 'matrix(0,1,-1,0,0,0)');
assert.equal(layer.children[0].dataset.start, '0');
renderTextLayer(node, layout);
assert.equal(node.surface.childElementCount, 1); // sem duplicação
node.surface.replaceChildren(); // lazy release / mudança de zoom
node.surface.clientWidth = 400;
assert.equal(renderTextLayer(node, layout), 3);
assert.equal(node.surface.children[0].children[0].style.transform, 'matrix(0.2,0,0,1,0,0)');
""")

    def test_annotation_failure_does_not_prevent_text_layer(self):
        self.run_js(r"""
const nodes = new Map([[12, node]]);
const generation = 1, firstPage = 12, activeExcerptId = null;
const root = {dataset: {layoutUrlTemplate:'layout', excerptsUrlTemplate:'excerpts'}};
const pageUrl = (template, number) => template;
const fetch = async url => url === 'layout' ? {ok:true, json:async () => layout} : {ok:false};
const renderExcerptOverlay = () => {}, renderSearchOverlays = () => {}, scheduleMargin = () => {};
(async () => {
  await decoratePage(12, 1);
  assert.equal(node.surface.children[0].childElementCount, 3);
  node.surface.replaceChildren();
  await decoratePage(12, 1);
  assert.equal(node.surface.childElementCount, 1);
  assert.equal(node.surface.children[0].childElementCount, 3);
})().catch(error => { console.error(error); process.exitCode = 1; });
""", decorate=True)

    def test_compact_controls_override_inherited_margin_and_align_buttons(self):
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        labels = css.split(".platform-qualitative-context-code-list label {", 1)[1].split("}", 1)[0]
        self.assertIn("margin: 0;", labels)
        self.assertIn("min-height: 1.65rem;", labels)
        actions = css.split(".platform-qualitative-record-new {", 1)[1].split("}", 1)[0]
        for rule in ("display: inline-flex", "align-items: center", "justify-content: center"):
            self.assertIn(rule, actions)

    def test_code_tag_contextmenu_dispatches_correct_ids_repeatedly_without_pdf_interception(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        self.assertIn("tag.dataset.codeId = code.id", SOURCE)
        self.assertIn("name.dataset.codeId = code.id", SOURCE)
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        self.assertIn("data-code-menu", template)
        self.assertIn("data-code-rename>Renomear código", template)
        code = SOURCE[SOURCE.index("  if (codeMenu && marginTrack) {"):
                      SOURCE.index("  const refreshPageExcerpts =")]
        checks = r"""
const assert=require('node:assert/strict');
const marginTrack=new EventTarget(),document=new EventTarget();
const root={dataset:{canAnnotate:'1'}};
const menuButton={focused:false,focus(){this.focused=true;},closest(selector){
  return selector==='[data-code-rename]'?this:null;}};
const codeMenu=new EventTarget();codeMenu.hidden=true;codeMenu.style={};
const actions={hidden:false},palette={hidden:true};
codeMenu.offsetWidth=150;codeMenu.offsetHeight=44;
codeMenu.querySelector=selector=>selector==='[data-code-rename]'?menuButton:
  selector==='[data-code-menu-actions]'?actions:selector==='[data-code-palette]'?palette:null;
codeMenu.querySelectorAll=()=>[];
const innerWidth=390,innerHeight=700;
let closed=0;const closeContextMenu=()=>closed++;
__CODE__
const requested=[];document.addEventListener('qualitative:rename-code-requested',event=>requested.push(event.detail.codeId));
const opened=[];document.addEventListener('platform:popover-open',event=>opened.push(event.detail.panel));
const label=id=>({dataset:{codeId:id},closest(selector){
  return selector==='.platform-qualitative-coding-tag[data-code-id]'?this:null;},
  getBoundingClientRect(){return {left:350,bottom:680};}});
const fire=(target,id)=>{const event=new Event('contextmenu',{cancelable:true});
  Object.defineProperty(event,'target',{value:target});event.clientX=380;event.clientY=690;
  marginTrack.dispatchEvent(event);return event;};
const ordinary={closest:()=>null};assert.equal(fire(ordinary).defaultPrevented,false);
for(const id of ['A','B','C']){
  const event=fire(label(id));assert.equal(event.defaultPrevented,true);
  assert.equal(codeMenu.hidden,false);assert.equal(menuButton.focused,true);
  assert.equal(parseInt(codeMenu.style.left,10)<=232,true);
  const click=new Event('click');Object.defineProperty(click,'target',{value:menuButton});
  codeMenu.dispatchEvent(click);assert.equal(codeMenu.hidden,true);
}
assert.deepEqual(requested,['A','B','C']);assert.equal(opened.length,3);
assert.equal(closed,3);
const keyboard=new Event('keydown',{cancelable:true});Object.defineProperty(keyboard,'target',{value:label('D')});
keyboard.key='F10';keyboard.shiftKey=true;marginTrack.dispatchEvent(keyboard);
assert.equal(keyboard.defaultPrevented,true);assert.equal(codeMenu.hidden,false);
"""
        result = subprocess.run([node, "-e", checks.replace("__CODE__", code)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
