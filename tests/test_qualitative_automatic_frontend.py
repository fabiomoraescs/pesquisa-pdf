"""Executa as ações reais do viewer em Node; nenhuma dependência browser nova."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")


class AutomaticFrontendTests(unittest.TestCase):
    def test_semantic_uses_shared_real_progress_overlay_and_restores_it(self):
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        helper = (ROOT / "static/js/qualitative_semantic_progress.js").read_text(encoding="utf-8")
        shared = (ROOT / "static/js/processing_progress.js").read_text(encoding="utf-8")
        self.assertIn("platform/_progress_overlay.html", template)
        self.assertIn("data-semantic-progress-url-template", template)
        self.assertIn("processing_progress.js", template)
        self.assertIn("startSemanticProgress(root)", SOURCE)
        self.run_js(r"""
const window={setTimeout,clearTimeout};
const values=new Map();
const node=id=>{if(!values.has(id)) values.set(id,{hidden:true,textContent:'',style:{},attrs:{},
  classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]}});
  return values.get(id)};
const document={getElementById:node};
const crypto={randomUUID:()=> '11111111-1111-4111-8111-111111111111'};
const root={dataset:{semanticProgressUrlTemplate:'/progress/00000000-0000-0000-0000-000000000000'}};
let requested='';
const fetch=async url=>{requested=url;return {ok:true,json:async()=>({percent:43,
  stage:'Processando e comparando segmentos',document:'Livro.pdf',page_number:8})}};
""" + shared + helper.replace("export function", "function") + r"""
(async()=>{
  const progress=startSemanticProgress(root);
  assert.equal(progress.id,'11111111-1111-4111-8111-111111111111');
  assert.equal(node('qualitative-semantic-overlay-processamento').hidden,false);
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(requested,'/progress/11111111-1111-4111-8111-111111111111');
  assert.equal(node('qualitative-semantic-progresso-processamento').attrs['aria-valuenow'],'43');
  assert.equal(node('qualitative-semantic-barra-progresso-processamento').style.width,'43%');
  assert.match(node('qualitative-semantic-detalhe-processamento').textContent,/43%/);
  assert.match(node('qualitative-semantic-mensagem-processamento').textContent,/Livro.pdf · página 8/);
  await progress.stop(true);
  assert.equal(node('qualitative-semantic-overlay-processamento').hidden,true);
  assert.equal(node('qualitative-semantic-progresso-processamento').attrs['aria-valuenow'],'100');
  const next=startSemanticProgress(root); await next.stop(false);
  assert.equal(node('qualitative-semantic-overlay-processamento').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1});
""")

    def test_enabled_modes_control_regex_without_changing_other_options(self):
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        help_text = (ROOT / "templates/platform/_qualitative_search_help.html").read_text(encoding="utf-8")
        for mode in ("literal", "lexical", "semantic"):
            self.assertIn(f'name="automatic_mode" value="{mode}"', template)
            self.assertNotIn(f'name="automatic_mode" value="{mode}" disabled', template)
        self.assertNotIn("Recurso ainda não disponível", help_text)
        self.assertIn("Regex fica indisponível", help_text)
        controls = SOURCE[SOURCE.index("  const regexInput ="):SOURCE.index("  searchForm.addEventListener('submit'")]
        self.run_js(r"""
const modes=['literal','lexical','semantic'].map(value=>({value,checked:value==='literal',
  addEventListener:(_,fn)=>{modes.find(item=>item.value===value).change=fn;}}));
const grep={checked:true,disabled:false};
const searchForm={querySelector:selector=>selector.includes('automatic_mode')?modes.find(item=>item.checked):grep,
  querySelectorAll:()=>modes,addEventListener(){}};
const automaticToggle={checked:false,addEventListener:(_,fn)=>automaticToggle.change=fn,
  setAttribute:(key,value)=>automaticToggle[key]=value};
const automaticOptions={hidden:true,disabled:true},automaticSettings={hidden:true};
const contextualRejection={disabled:true,checked:false},multipleTerms={disabled:true,checked:false};
const window={TermInput:{validate:()=>true}},searchInput={},separatorError={};
""" + controls + r"""
assert.equal(grep.checked,true);assert.equal(grep.disabled,false);
automaticToggle.checked=true;automaticToggle.change();
assert.equal(contextualRejection.disabled,false);assert.equal(multipleTerms.disabled,false);
modes[0].checked=false;modes[1].checked=true;modes[1].change();
assert.equal(selectedAutomaticMode(),'lexical');assert.equal(grep.checked,false);assert.equal(grep.disabled,true);
modes[1].checked=false;modes[2].checked=true;modes[2].change();
assert.equal(selectedAutomaticMode(),'semantic');assert.equal(grep.disabled,true);
modes[2].checked=false;modes[0].checked=true;modes[0].change();
assert.equal(selectedAutomaticMode(),'literal');assert.equal(grep.disabled,false);assert.equal(grep.checked,false);
automaticToggle.checked=false;automaticToggle.change();
assert.equal(grep.disabled,false);
assert.equal(contextualRejection.disabled,true);assert.equal(multipleTerms.disabled,true);
""")

    def test_focus_navigation_reuses_buttons_and_works_while_minimized(self):
        navigation = SOURCE[SOURCE.index("  const showResult ="):SOURCE.index("  const regexInput =")]
        moving = SOURCE[SOURCE.index("  const arrangeResultNavigation ="):SOURCE.index("  const arrangeAutomaticControls =")]
        minimizing = SOURCE[SOURCE.index("  const updateFocusPanel ="):SOURCE.index("  const arrangeResultNavigation =")]
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        header = template.split('class="platform-qualitative-focus-panel-header"', 1)[1].split('data-explorer-toggle', 1)[0]
        self.assertIn('data-focus-navigation', header)
        self.assertIn('data-focus-panel-toggle', header)
        self.assertIn('aria-live="polite"', header)
        for control in ("data-result-prev", "data-result-count", "data-result-next"):
            self.assertEqual(template.count(control), 1)
        self.assertIn("arrangeResultNavigation(focused);", SOURCE)
        self.run_js(r"""
class Element {
  constructor(){this.children=[];this.listeners={};this.classes=new Set();
    this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name), contains:name=>this.classes.has(name)};}
  append(...children){children.forEach(child=>this.insertBefore(child,null));}
  insertBefore(child,before){if(child.parent)child.parent.children=child.parent.children.filter(item=>item!==child);
    const index=before?this.children.indexOf(before):this.children.length;assert.ok(index>=0);
    this.children.splice(index,0,child);child.parent=this;}
  addEventListener(name,fn){assert.equal(this.listeners[name],undefined);this.listeners[name]=fn;}
  click(){if(!this.disabled)this.listeners.click();}
  setAttribute(name,value){this[name]=value;}
}
const prev=new Element(),next=new Element(),resultCount=new Element(),searchMessage=new Element(),resultSnippet=new Element();
const searchResults=new Element(); searchResults.append(prev,resultCount,next,searchMessage);
const focusNavigation=new Element(),focusPanel=new Element(),focusPanelToggle=new Element();
const body=new Element();body.append(searchResults,resultSnippet);focusPanel.append(focusNavigation,focusPanelToggle,body);
const focusActive=()=>true,window={matchMedia:()=>({matches:true})},closeExplorerPopovers=()=>{};
let results=[{page_number:1,snippet:'Primeiro'},{page_number:2,snippet:'Segundo'}],resultIndex=-1,highlightPage;
const generation=1,nodes=new Map([[1,{rendered:1}],[2,{rendered:1}]]);
const renderSearchOverlays=number=>{if(number===results[resultIndex].page_number)highlightPage=number;},goToPage=()=>{};
""" + navigation + moving + minimizing + r"""
showResult(0);
for(const focused of [true,false,true,false,true]) {
  arrangeResultNavigation(focused);
  assert.deepEqual(focusNavigation.children,focused?[prev,resultCount,next]:[]);
  assert.deepEqual(searchResults.children,focused?[searchMessage]:[prev,resultCount,next,searchMessage]);
  assert.equal(resultSnippet.parent,body);assert.equal(resultCount.parent,focused?focusNavigation:searchResults);
}
focusPanelToggle.click();assert.equal(focusPanel.classList.contains('is-minimized'),true);
next.click();assert.equal(resultIndex,1);assert.equal(highlightPage,2);
assert.equal(resultCount.textContent,'2 de 2');assert.equal(resultCount.parent,focusNavigation);
assert.equal(resultSnippet.textContent,'Página 2: …Segundo…');
prev.click();assert.equal(resultIndex,0);assert.equal(highlightPage,1);
assert.equal(resultCount.textContent,'1 de 2');
next.click();focusPanelToggle.click();assert.equal(focusPanel.classList.contains('is-minimized'),false);
assert.equal(resultCount.textContent,'2 de 2');assert.equal(resultSnippet.textContent,'Página 2: …Segundo…');
prev.disabled=true;arrangeResultNavigation(false);arrangeResultNavigation(true);assert.equal(prev.disabled,true);
""")

    def test_help_buttons_use_decorative_info_circle_without_changing_popover_contract(self):
        from html.parser import HTMLParser
        from jinja2 import Environment, FileSystemLoader

        class Tags(HTMLParser):
            def __init__(self):
                super().__init__(); self.items = []
            def handle_starttag(self, tag, attrs):
                self.items.append((tag, dict(attrs)))

        template = Environment(loader=FileSystemLoader(ROOT / "templates")).get_template(
            "platform/_qualitative_search_help.html").module
        labels = {"regex": "Regex", "case": "Maiúsculas e minúsculas", "automatic": "Autocodificação",
                  "multiple": "Múltiplos termos", "rejection": "Rejeição contextual", "literal": "autocodificação Literal",
                  "lexical": "autocodificação Lexical", "semantic": "autocodificação Semântica"}
        for key, label in labels.items():
            with self.subTest(control=key):
                html = str(template.help_button(key))
                tags = Tags(); tags.feed(html)
                self.assertEqual([tag for tag, _ in tags.items], ["button", "svg", "circle", "path"])
                button, svg, circle = (attrs for _, attrs in tags.items[:3])
                self.assertEqual(button["type"], "button")
                self.assertEqual(button["aria-label"], f"Sobre {label}")
                self.assertEqual(button["popovertarget"], f"qualitative-help-{key}")
                self.assertEqual(button["aria-controls"], button["popovertarget"])
                self.assertEqual(button["aria-expanded"], "false")
                self.assertNotIn("disabled", button)
                self.assertEqual(svg["aria-hidden"], "true")
                self.assertEqual(svg["focusable"], "false")
                self.assertEqual(circle, {"cx": "12", "cy": "12", "r": "9"})
                self.assertNotIn("(i)", html)
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        button_css = css.split(".platform-qualitative-help-button {", 1)[1].split("}", 1)[0]
        self.assertIn("align-self: center", button_css)
        self.assertIn("min-width: 1.5rem", button_css)
        self.assertIn("justify-content: flex-start", button_css)
        self.assertIn("padding: 0", button_css)
        control_css = css.split(".platform-qualitative-search-control {", 1)[1].split("}", 1)[0]
        self.assertIn("gap: 0", control_css)
        icon_css = css.split(".platform-qualitative-help-button svg {", 1)[1].split("}", 1)[0]
        self.assertIn("width: .75rem", icon_css)
        self.assertIn("height: .75rem", icon_css)
        self.assertIn("stroke: currentColor", icon_css)
        self.assertIn(".platform-qualitative-help-button:hover", css)
        self.assertIn(".platform-qualitative-help-button:focus-visible", css)

    def run_js(self, code):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run([node, "-e", "const assert = require('node:assert/strict');\n" + code],
                                capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_toggle_off_only_searches_and_on_runs_directly_without_code_dialog(self):
        source = SOURCE[SOURCE.index("  automaticToggle?.addEventListener"):SOURCE.index("  const selectionBoundary =")]
        self.run_js(r"""
const searchForm = {handler:null, addEventListener:(_, fn) => searchForm.handler=fn};
const automaticToggle = {checked:false, addEventListener:(_, fn) => automaticToggle.handler=fn,
  setAttribute:(key,value) => automaticToggle[key]=value};
const automaticOptions = {hidden:true,disabled:true}, contextualRejection={checked:false,disabled:true}, multipleTerms={checked:false,disabled:true};
const automaticSettings = {hidden:true};
const searchSubmit={disabled:false};
const fields = new Map([['q','Raça'],['grep','on'],['case','on'],['automatic_scope','project']]);
const FormData = class {get(key){return fields.get(key);} has(key){return fields.has(key);}};
let automaticBusy=false, automaticSnapshot, searchRequest=0, results=[], resultIndex=-1;
const resultCount={}, resultSnippet={}, prev={}, next={}, nodes=new Map();
let notice='', opened='', calls=[];
const searchNotice = message => notice=message;
const root = {dataset:{searchUrl:'/buscar'}};
const fetch = async (url, options) => {calls.push({url,options}); assert.equal(searchSubmit.disabled,true); return {ok:true,json:async () => ({results:[],total:0})};};
let validTerms=true;
const validateAutomaticTerms=() => validTerms, syncRegexMode=()=>{}, selectedAutomaticMode=()=> 'literal', searchInput={focus(){}};
const showResult = () => {}, openContextDialog = () => {throw Error('Não abrir modal');};
const sendAutomatic=async () => calls.push({url:'/codificar',body:automaticSnapshot});
""" + source + r"""
(async () => {
  await searchForm.handler({preventDefault(){}});
  assert.equal(calls.length,1); assert.ok(calls[0].url.startsWith('/buscar?'));
  assert.equal(calls[0].options.method,undefined); assert.equal(opened,'');
  assert.equal(searchSubmit.disabled,false);
  automaticToggle.checked=true; automaticToggle.handler();
  assert.equal(automaticOptions.hidden,false); assert.equal(automaticOptions.disabled,false);
  assert.equal(automaticSettings.hidden,false);
  assert.equal(contextualRejection.checked,false); assert.equal(contextualRejection.disabled,false);
  assert.equal(multipleTerms.checked,false); assert.equal(multipleTerms.disabled,false);
  contextualRejection.checked=true;
  multipleTerms.checked=true;
  assert.equal(automaticToggle['aria-expanded'],'true');
  await searchForm.handler({preventDefault(){}});
  assert.equal(calls.length,2); assert.equal(calls[1].url,'/codificar'); assert.equal(opened,'');
  assert.equal('code_id' in calls[1].body,false); assert.equal('name' in calls[1].body,false);
  assert.deepEqual(automaticSnapshot,{q:'Raça',grep:true,case_sensitive:true,mode:'literal',scope:'project',contextual_rejection_enabled:true,multiple_terms:true});
  automaticToggle.checked=false; automaticToggle.handler();
  assert.equal(automaticOptions.hidden,true); assert.equal(automaticOptions.disabled,true);
  assert.equal(automaticSettings.hidden,true);
  assert.equal(contextualRejection.checked,false); assert.equal(contextualRejection.disabled,true);
  assert.equal(multipleTerms.checked,false); assert.equal(multipleTerms.disabled,true);
  assert.equal(automaticSnapshot.multiple_terms,true);
  assert.equal(automaticSnapshot.contextual_rejection_enabled,true); // a operação guarda sua própria escolha
  validTerms=false;
  await searchForm.handler({preventDefault(){}});
  assert.equal(calls.length,2); // aviso bloqueia envio, sem persistência
})().catch(error => {console.error(error);process.exitCode=1;});
""")

    def test_consultative_search_navigates_and_highlights_all_3000_results(self):
        overlays = SOURCE[SOURCE.index("  const renderSearchOverlays ="):SOURCE.index("  const decoratePage =")]
        navigation = SOURCE[SOURCE.index("  const showResult ="):SOURCE.index("  const selectionBoundary =")]
        self.run_js(r"""
class Element {
  constructor(){this.children=[];this.dataset={};}
  append(child){child.parent=this;this.children.push(child);}
  querySelector(selector){return this.children.find(child => selector==='.'+child.className);}
  remove(){this.parent.children=this.parent.children.filter(child => child!==this);}
}
const document={createElement:() => new Element()}, generation=1;
const nodes=new Map([1,2].map(number => [number,{rendered:1,surface:new Element(),layout:{page_text_hash:'hash'+number}}]));
const exactItems=(_,start,end) => [{bbox:[start,0,end,1]}], mergeGeometry=items => items;
const placeBox=(_,__,className) => Object.assign(new Element(),{className});
const requestAnimationFrame=fn => fn(), scrollToActive=() => {}, goToPage=() => {throw Error('Already rendered');};
const prev={addEventListener:(_,fn) => prev.click=fn}, next={addEventListener:(_,fn) => next.click=fn};
const grep={checked:false};
const searchForm={querySelector:()=>grep,querySelectorAll:()=>[],
  addEventListener:(name,fn) => {if(name==='submit')searchForm.submit=fn;}}, searchSubmit={disabled:false};
const automaticToggle={checked:false,addEventListener(){}};
const automaticOptions={},automaticSettings={},contextualRejection={checked:false};
const window={TermInput:{validate:() => true}},searchInput={},separatorError={},multipleTerms={checked:false};
const FormData=class {get(){return 'Ação 😀';} has(){return false;}};
const all=Array.from({length:3000},(_,index) => ({document_id:'own',page_number:1+Math.floor(index/1500),
  start_offset:(index%1500)*7,end_offset:(index%1500)*7+6,snippet:'Ação 😀',page_text_hash:'hash'+(1+Math.floor(index/1500))}));
let results=[],resultIndex=-1,searchRequest=0,automaticBusy=false;
const resultCount={},resultSnippet={},root={dataset:{searchUrl:'/buscar'}},searchNotice=() => {};
const fetch=async () => ({ok:true,json:async () => ({results:all,total:3000})});
""" + overlays + navigation + r"""
(async () => {
  await searchForm.submit({preventDefault(){}});
  assert.equal(results.length,3000); assert.equal(resultCount.textContent,'1 de 3000');
  assert.equal(searchSubmit.disabled,false);
  assert.equal(nodes.get(1).surface.children[0].children.length,1500);
  assert.equal(nodes.get(2).surface.children[0].children.length,1500);
  showResult(199); next.click(); assert.equal(resultCount.textContent,'201 de 3000');
  showResult(2998); next.click(); assert.equal(resultCount.textContent,'3000 de 3000');
  assert.equal(nodes.get(2).surface.children[0].children[1499].dataset.searchIndex,'2999');
  assert.ok(nodes.get(2).surface.children[0].children[1499].className.includes('is-active'));
  next.click(); assert.equal(resultCount.textContent,'1 de 3000');
  prev.click(); assert.equal(resultCount.textContent,'3000 de 3000');
  assert.equal(resultSnippet.textContent,'Página 2: …Ação 😀…');
})().catch(error => {console.error(error);process.exitCode=1;});
""")

    def test_controls_use_two_normal_rows_and_three_focus_rows(self):
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        options = css.split(".platform-qualitative-search-options {", 1)[1].split("}", 1)[0]
        self.assertIn("display: flex", options)
        self.assertIn("flex-wrap: wrap", options)
        self.assertIn("align-items: center", options)
        modes = css.split(".platform-qualitative-automatic-options {", 1)[1].split("}", 1)[0]
        self.assertIn("display: flex", modes)
        self.assertIn("flex-wrap: wrap", modes)
        self.assertIn("min-width: 0", modes)
        self.assertNotIn("width: 100%", modes)
        self.assertNotIn("platform-qualitative-mode-separator", css)
        self.assertIn(".platform-qualitative-focus .platform-panel.platform-qualitative-top:has([data-automatic-toggle]) { width: min(29rem, calc(100vw - 2rem)); }", css)
        self.assertIn(".platform-qualitative-search-options.platform-qualitative-automatic-settings { display: contents; }", css)
        self.assertIn(".platform-qualitative-focus .platform-qualitative-automatic-options { flex-direction: column; align-items: stretch; margin-top: 0; }", css)
        self.assertIn(".platform-qualitative-focus .platform-qualitative-automatic-settings { display: flex; }", css)
        self.assertIn(".platform-qualitative-focus .platform-qualitative-automatic-modes { column-gap: .25rem; }", css)
        self.assertIn(".platform-qualitative-focus .platform-qualitative-automatic-scope { flex: 1 1 12rem; margin-top: 0; }", css)
        scope_select = css.split(".platform-qualitative-automatic-scope select {", 1)[1].split("}", 1)[0]
        self.assertIn("padding-right: 1.75rem", scope_select)
        self.assertIn(".platform-qualitative-automatic-options[hidden] { display: none; }", css)
        self.assertIn(".platform-qualitative-search-options.platform-qualitative-automatic-settings[hidden] { display: none; }", css)

    def test_checkbox_and_radio_switches_share_compact_states_without_changing_native_behavior(self):
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        switch = css.split("input.platform-qualitative-switch {", 1)[1].split("}", 1)[0]
        for rule in ("appearance: none", "width: 1.5rem", "height: .875rem", "margin: 0", "padding: 0", "flex: none"):
            self.assertIn(rule, switch)
        knob = css.split(".platform-qualitative-switch::before {", 1)[1].split("}", 1)[0]
        checked = css.split(".platform-qualitative-switch:checked::before {", 1)[1].split("}", 1)[0]
        self.assertIn("left: .1rem", knob)
        self.assertIn("width: .6rem", knob)
        self.assertIn("left: .775rem", checked)  # posição também distingue ON/OFF, não só cor
        self.assertIn(".platform-qualitative-switch:disabled { opacity: .5; cursor: not-allowed; }", css)
        self.assertIn("input.platform-qualitative-switch:hover:not(:disabled)", css)
        self.assertIn("input.platform-qualitative-switch:focus-visible { outline: 2px solid var(--blue)", css)

    def test_focus_reuses_same_controls_and_preserves_values_and_keyboard_order(self):
        source = SOURCE[SOURCE.index("  const arrangeAutomaticControls ="):SOURCE.index("  focusToggle.addEventListener")]
        self.assertIn("arrangeAutomaticControls(focused);", SOURCE)
        self.run_js(r"""
class Container {
  constructor(children=[]){this.children=[];children.forEach(child=>this.append(child));}
  get firstElementChild(){return this.children[0] || null;}
  append(child){this.insertBefore(child,null);}
  insertBefore(child,before){
    if(child.parent) child.parent.children=child.parent.children.filter(item=>item!==child);
    const index=before?this.children.indexOf(before):this.children.length;
    assert.ok(index>=0); this.children.splice(index,0,child);child.parent=this;
  }
}
const base=[{name:'grep',checked:true},{name:'case',checked:true},{name:'automatic',checked:true}];
const multiple={name:'multiple_terms',checked:true}, rejection={name:'rejection',checked:true};
const automaticSettings=new Container([multiple,rejection]);
const modes=new Container([{name:'literal',checked:true},{name:'lexical',disabled:false},{name:'semantic',disabled:false}]);
const automaticModes=modes, multipleControl=multiple;
const automaticScope={name:'scope',value:'project'};
const searchControlRow=new Container([...base,automaticSettings]);
const automaticOptions=new Container([modes,automaticScope]);
const flatten=container=>container.children.flatMap(item=>item.children?flatten(item):[item]);
const original=[...flatten(searchControlRow),...flatten(automaticOptions)];
const snapshot=original.map(item=>({name:item.name,checked:item.checked,value:item.value,disabled:item.disabled}));
""" + source + r"""
for(const focused of [true,false,true,false]) {
  arrangeAutomaticControls(focused);
  assert.deepEqual(searchControlRow.children,focused?base:[...base,automaticSettings]);
  assert.deepEqual(automaticOptions.children,focused?[modes,automaticSettings]:[modes,automaticScope]);
  assert.deepEqual(modes.children.map(item=>item.name),focused?['literal','lexical','semantic','multiple_terms']:['literal','lexical','semantic']);
  assert.deepEqual(automaticSettings.children,focused?[rejection,automaticScope]:[multiple,rejection]);
  const controls=[...flatten(searchControlRow),...flatten(automaticOptions)];
  assert.deepEqual(controls.map(item=>item.name),focused
    ?['grep','case','automatic','literal','lexical','semantic','multiple_terms','rejection','scope']
    :['grep','case','automatic','multiple_terms','rejection','literal','lexical','semantic','scope']);
  assert.equal(controls.length,original.length);
  assert.deepEqual(new Set(controls),new Set(original));
  assert.deepEqual(original.map(item=>({name:item.name,checked:item.checked,value:item.value,disabled:item.disabled})),snapshot);
}
""")

    def test_direct_automatic_request_uses_query_records_event_and_document_navigation(self):
        source = SOURCE[SOURCE.index("  const sendAutomatic ="):SOURCE.index("  if (contextMenu && contextDialog)")]
        navigation = SOURCE[SOURCE.index("  const showResult ="):SOURCE.index("  const regexInput =")]
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        self.assertNotIn("data-automatic-summary", template)
        self.assertNotIn("automaticSummary", SOURCE)
        for marker in ("data-result-prev", "data-result-count", "data-result-next", "data-result-snippet", "data-search-message"):
            self.assertIn(marker, template)
        self.run_js(r"""
let automaticBusy=false, results=[], resultIndex=-1, notice='';
const automaticSnapshot={q:'termo',mode:'literal',scope:'project',grep:false,case_sensitive:false,contextual_rejection_enabled:true,multiple_terms:true};
const resultCount={}, resultSnippet={}, searchSubmit={};
const prev={addEventListener:(_,fn)=>prev.click=fn}, next={addEventListener:(_,fn)=>next.click=fn};
const surface={querySelector:()=>null}, generation=1;
const nodes=new Map([[1,{rendered:1,surface}]]), root={dataset:{automaticUrl:'/codificar',documentId:'own'},querySelector:() => ({value:'csrf'})};
const events=[]; const document={dispatchEvent:event => events.push(event)};
const CustomEvent=class {constructor(type, options){this.type=type;this.detail=options.detail;}};
const searchNotice=message => notice=message, renderSearchOverlays=()=>searchNotice(''), goToPage=()=>{};
let request;
const fetch=async (url, options) => {request={url,options}; return {ok:true,json:async () => ({
  search:{results:[{document_id:'own',page_number:1,snippet:'Primeiro trecho'},
    {document_id:'own',page_number:1,snippet:'Segundo trecho'},{document_id:'other',page_number:1}],total:3},
  // O resumo continua na API, mas o leitor não deve sequer consumi-lo para apresentação.
  get summary(){throw Error('Não renderizar resumo estatístico');},records:{codes:[{id:'code'}],memos:[]}})};};
""" + navigation + source + r"""
(async () => {
  for(const focused of [false,true]) {
    document.body={className:focused?'platform-qualitative-focus':''};
    await sendAutomatic();
    assert.equal(request.options.method,'POST'); assert.equal(request.options.headers['X-CSRFToken'],'csrf');
    assert.equal('code_id' in JSON.parse(request.options.body),false);
    assert.equal('name' in JSON.parse(request.options.body),false); assert.equal(results.length,2);
    assert.equal(JSON.parse(request.options.body).contextual_rejection_enabled,true);
    assert.equal(JSON.parse(request.options.body).multiple_terms,true);
    assert.equal(events.at(-1).type,'qualitative:records-updated');
    assert.equal(resultCount.textContent,'1 de 2');
    assert.equal(resultSnippet.textContent,'Página 1: …Primeiro trecho…');
    assert.equal(prev.disabled,false); assert.equal(next.disabled,false);
    next.click(); assert.equal(resultCount.textContent,'2 de 2');
    assert.equal(resultSnippet.textContent,'Página 1: …Segundo trecho…');
    prev.click(); assert.equal(resultCount.textContent,'1 de 2');
    assert.equal(notice,''); assert.equal(automaticBusy,false); assert.equal(searchSubmit.disabled,false);
  }
})().catch(error => {console.error(error);process.exitCode=1;});
""")

    def test_automatic_errors_remain_visible_without_summary(self):
        source = SOURCE[SOURCE.index("  const sendAutomatic ="):SOURCE.index("  if (contextMenu && contextDialog)")]
        self.run_js(r"""
let automaticBusy=false, notice='', error;
const automaticSnapshot={q:'['}, searchSubmit={}, root={dataset:{automaticUrl:'/codificar'},querySelector:()=>({value:'csrf'})};
const searchNotice=message=>notice=message;
const fetch=async()=>({ok:false,json:async()=>({error})});
""" + source + r"""
(async()=>{
  for(error of ['Expressão regular inválida.','Consulta inválida.','Erro de processamento.',undefined]) {
    await sendAutomatic();
    assert.equal(notice,error || 'Não foi possível concluir a codificação. Nenhuma alteração foi confirmada.');
    assert.equal(automaticBusy,false); assert.equal(searchSubmit.disabled,false);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_shared_separator_notice_blocks_literals_but_not_regex_or_single_query(self):
        shared = (ROOT / "static/js/term_input.js").read_text(encoding="utf-8")
        source = SOURCE[SOURCE.index("  const regexInput ="):SOURCE.index("  automaticToggle?.addEventListener")]
        self.run_js(r"""
const window={};
""" + shared + r"""
const attrs={},searchInput={value:'racismo, raça',setAttribute:(k,v)=>attrs[k]=v,removeAttribute:k=>delete attrs[k]};
const separatorError={hidden:true},automaticToggle={checked:true},multipleTerms={checked:true},grep={checked:false};
const searchForm={querySelector:()=>grep,querySelectorAll:()=>[],addEventListener(){}};
""" + source + r"""
for(const value of ['racismo, raça','racismo. raça','racismo,raça']) {
  searchInput.value=value; assert.equal(validateAutomaticTerms(),false);
  assert.equal(attrs['aria-invalid'],'true'); assert.equal(separatorError.hidden,false);
  assert.equal(searchInput.value,value); // não autocorrige
}
searchInput.value='racismo; raça';assert.equal(validateAutomaticTerms(),true);
assert.equal(separatorError.hidden,true);assert.equal('aria-invalid' in attrs,false);
searchInput.value='racis.*;discrimin.{1,3}';grep.checked=true;assert.equal(validateAutomaticTerms(),true);
grep.checked=false;multipleTerms.checked=false;assert.equal(validateAutomaticTerms(),true);
multipleTerms.checked=true;automaticToggle.checked=false;assert.equal(validateAutomaticTerms(),true);
""")
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        self.assertIn('class="terms-separator-error"', template)
        self.assertIn('data-separator-error role="alert" hidden', template)
        self.assertIn('aria-describedby="qualitative-separator-error"', template)
        scraping = (ROOT / "templates/index.html").read_text(encoding="utf-8")
        self.assertIn('window.TermInput.validate(campoTermos, erroSeparadorTermos', scraping)
        self.assertIn("js/term_input.js", scraping)

    def test_manual_code_dialog_still_opens_with_selection_and_creation(self):
        source = SOURCE[SOURCE.index("  const openContextDialog ="):SOURCE.index("  const sendContext =")]
        self.run_js(r"""
let contextAction,selectedCodeIds,opened=0,choices=0;
const fields=new Map();
const contextDialog={querySelector:key=>{if(!fields.has(key)) fields.set(key,{});return fields.get(key);},showModal:()=>opened++};
const contextSnapshot={page_number:1,start:0,end:7,page_hash:'hash',selected_text:'racismo'};
const contextError={},contextFilter={focus(){}},memoText={focus(){}},nodes=new Map([[1,{excerpts:[]}]]);
const closeContextMenu=()=>{},renderCodeChoices=()=>choices++;
""" + source + r"""
openContextDialog('apply_codes','Novo código');
assert.equal(opened,1);assert.equal(choices,1);assert.equal(contextFilter.value,'Novo código');
assert.equal(fields.get('[data-context-title]').textContent,'Aplicar código');
assert.equal(fields.get('[data-context-codes]').hidden,false);
assert.equal(fields.get('[data-context-save]').textContent,'Salvar');
""")
        self.assertNotIn("openContextDialog('automatic')", SOURCE)
        self.assertNotIn("contextAction === 'automatic'", SOURCE)
        self.assertIn("await sendContext('create_and_apply', { name, description: '' });", SOURCE)

    def test_margin_tags_have_individual_accessible_buttons_and_origin_without_nested_buttons(self):
        source = SOURCE[SOURCE.index("  const renderMargin ="):SOURCE.index("  marginTrack?.addEventListener")]
        self.run_js(r"""
const make = tag => ({tag,children:[],style:{setProperty(name,value){this[name]=value;}},classList:{toggle(){}},dataset:{},attrs:{},
  append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},setAttribute(k,v){this.attrs[k]=v;}});
const document={createElement:make}, window={matchMedia:() => ({matches:true})};
const marginTrack=make('div'), margin={style:{}}, marginViewport={style:{}}, generation=1, activeExcerptId='excerpt';
const root={dataset:{canAnnotate:'1'}}, removingCodings=new Set();
const excerpt={id:'excerpt',start:0,end:3,memo_count:1,codes:[
  {id:'a',coding_id:'coding-a',name:'A',origin:'manual'},
  {id:'b',coding_id:'coding-b',name:'B',origin:'automatic_literal'}]};
const nodes=new Map([[1,{rendered:1,layout:{layout_available:true,height:100},excerpts:[excerpt],
  shell:{offsetTop:0},surface:{offsetTop:0,clientHeight:100}}]]);
const exactItems=() => [{bbox:[0,2,3,4]}], syncMarginScroll=() => {};
""" + source + r"""
renderMargin();
const card=marginTrack.children[0]; assert.equal(card.tag,'div'); assert.equal(card.attrs.role,'group');
assert.equal(card.children[0].children[1].dataset.removeCoding,'coding-a');
assert.equal(card.children[1].children[1].dataset.removeCoding,'coding-b');
assert.ok(card.children[1].title.includes('Literal'));
assert.equal(card.children[0].children[1].attrs['aria-label'],'Remover codificação A');
root.dataset.canAnnotate='0'; renderMargin();
assert.equal(marginTrack.children[0].children[0].children.length,1);
""")

    def test_remove_button_only_deletes_coding_and_updates_the_same_page_overlays(self):
        source = SOURCE[SOURCE.index("  marginTrack?.addEventListener"):SOURCE.index("  if (codeMenu && marginTrack) {")]
        self.run_js(r"""
let handler, activeExcerptId='excerpt', automaticBusy=false, drawn=0, scheduled=0, moved=0;
const removingCodings=new Set(), marginTrack={addEventListener:(_,fn) => handler=fn};
const remove={dataset:{removeCoding:'coding-b'},disabled:false};
const card={dataset:{excerptId:'excerpt',pageNumber:'1'}};
const event={target:{closest:s => s==='[data-remove-coding]'?remove:card},stopPropagation(){}};
const node={rendered:1,excerpts:[]},nodes=new Map([[1,node]]),generation=1;
const root={dataset:{deleteCodingUrlTemplate:'/codificacoes/00000000-0000-0000-0000-000000000000'},querySelector:() => ({value:'csrf'})};
let request, notice; const events=[];
const document={dispatchEvent:event => events.push(event)};
const CustomEvent=class {constructor(type,options){this.type=type;this.detail=options.detail;}};
const remaining=[{id:'excerpt',codes:[{id:'a',coding_id:'coding-a'}],memo_count:1}];
const fetch=async (url,options) => {request={url,options}; return {ok:true,json:async () => ({page_number:1,
  page:{excerpts:remaining},records:{codes:[{id:'a'}],memos:[{id:'memo'}]}})};};
const renderExcerptOverlay=() => drawn++, scheduleMargin=() => scheduled++, noticeContext=m => notice=m;
const activateExcerpt=() => moved++;
""" + source + r"""
(async () => {
  await handler(event);
  assert.equal(request.url,'/codificacoes/coding-b'); assert.equal(request.options.method,'DELETE');
  assert.equal(request.options.headers['X-CSRFToken'],'csrf'); assert.deepEqual(node.excerpts,remaining);
  assert.equal(drawn,1); assert.equal(scheduled,1); assert.equal(moved,0); assert.equal(activeExcerptId,'excerpt');
  assert.equal(events[0].type,'qualitative:records-updated'); assert.equal(remove.disabled,false);
  remaining.length=0; await handler(event); assert.equal(activeExcerptId,null);
})().catch(error => {console.error(error);process.exitCode=1;});
""")
