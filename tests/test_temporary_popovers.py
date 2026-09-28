"""Executa a regra compartilhada de popovers em Node com DOM/eventos mínimos."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/temporary_popovers.js").read_text(encoding="utf-8")
DOM = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag, id, parent=null) {
    this.tagName=tag; this.id=id; this.parent=parent; this.listeners={}; this.attrs={};
    this.hidden=true; this._open=false; this.native=false; this.files=[]; this.style={};
    this.offsetWidth=260; this.offsetHeight=100;
    this.rect={left:350,right:375,top:780,bottom:804};
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  emit(name, fields={}) { for (const fn of this.listeners[name] || []) fn({target:this, ...fields}); }
  contains(target) { return target === this || Boolean(target?.parent && this.contains(target.parent)); }
  setAttribute(key, value) { this.attrs[key]=value; }
  hasAttribute(key) { return (key === 'popover' && this.native) || key in this.attrs; }
  getClientRects() { return [this.rect]; }
  getBoundingClientRect() { return this.rect; }
  querySelector(selector) { return selector === 'summary' ? this.trigger : null; }
  querySelectorAll(selector) { return selector === 'input[type="file"]' ? this.files : []; }
  matches() { return this._open; }
  get open() { return this._open; }
  set open(value) { this._open=value; this.emit('toggle'); }
  showPopover() { this.emit('beforetoggle', {newState:'open'}); this._open=true; this.emit('toggle'); }
  hidePopover() { this._open=false; this.emit('toggle'); }
  focus() { document.activeElement=this; document.emit('focusin', {target:this}); }
}
const document = new Element('DOCUMENT', 'doc');
document.hasFocus = () => true;
const upload = new Element('DETAILS','upload');
upload.trigger = new Element('SUMMARY', 'upload-trigger', upload);
const file = new Element('INPUT','file',upload);
upload.files=[file];
const inner = new Element('BUTTON','upload-submit', upload);
const docs = new Element('DIV','documents'); docs.native=true;
const codes = new Element('DIV','codes'); codes.native=true;
const context = new Element('DIV','context');
const docsTrigger = new Element('BUTTON','docs-trigger'); docsTrigger.attrs['aria-controls']='documents';
const codesTrigger = new Element('BUTTON','codes-trigger'); codesTrigger.attrs['aria-controls']='codes';
const docLink = new Element('A','document-link',docs);
const contextAction = new Element('BUTTON','context-action',context);
const outside = new Element('BUTTON','outside');
const dialog = new Element('DIALOG','editor'); dialog.hidden=false;
const helpRegex=new Element('DIV','help-regex'), helpLexical=new Element('DIV','help-lexical');
const helpRegexTrigger=new Element('BUTTON','help-regex-trigger'), helpLexicalTrigger=new Element('BUTTON','help-lexical-trigger');
for (const [panel,trigger] of [[helpRegex,helpRegexTrigger],[helpLexical,helpLexicalTrigger]]) {
  panel.native=true; panel.attrs['data-popover-anchor']=''; trigger.attrs['aria-controls']=panel.id;
}
const window=new Element('WINDOW','window'); window.innerWidth=390; window.innerHeight=844;
const panels=[upload,docs,codes,context,helpRegex,helpLexical], triggers=[docsTrigger,codesTrigger,helpRegexTrigger,helpLexicalTrigger];
document.querySelectorAll = selector => selector === '[data-temporary-popover]' ? panels :
  triggers.filter(t => `[aria-controls="${t.attrs['aria-controls']}"]` === selector);
"""


class TemporaryPopoverTests(unittest.TestCase):
    def run_js(self, checks):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run([node, "-e", DOM + SOURCE + checks], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_popovers_share_dismissal_and_stay_inside_viewport_when_anchor_moves(self):
        self.run_js(r"""
helpRegexTrigger.focus(); helpRegex.showPopover();
assert.equal(helpRegexTrigger.attrs['aria-expanded'],'true');
assert.equal(helpRegex.style.left,'122px'); assert.equal(helpRegex.style.top,'676px');
document.documentElement={clientWidth:375,clientHeight:844}; window.emit('resize');
assert.equal(helpRegex.style.left,'107px'); assert.equal(helpRegex.style.maxWidth,'359px');
helpRegex.focus(); document.emit('click',{target:helpRegex}); assert.equal(helpRegex.matches(),true);
helpLexicalTrigger.focus(); helpLexical.showPopover();
assert.equal(helpRegex.matches(),false); assert.equal(helpLexical.matches(),true);
helpLexicalTrigger.rect={left:10,right:30,top:10,bottom:34};
document.emit('pointermove',{buttons:1});
assert.equal(helpLexical.style.left,'10px'); assert.equal(helpLexical.style.top,'38px');
document.emit('keydown',{key:'Escape'}); assert.equal(helpLexical.matches(),false);
assert.equal(document.activeElement,helpLexicalTrigger);
helpRegex.showPopover(); document.emit('click',{target:outside}); assert.equal(helpRegex.matches(),false);
helpRegex.showPopover(); docs.showPopover(); assert.equal(helpRegex.matches(),false);
""")

    def test_internal_interaction_and_trigger_keep_open_but_outside_click_closes(self):
        self.run_js(r"""
upload.open=true;
for (const target of [upload.trigger, file, inner]) {
  document.emit('pointerdown', {target}); document.emit('click', {target}); target.focus();
  assert.equal(upload.open,true);
}
document.emit('click',{target:outside});
assert.equal(upload.open,false);
assert.equal(upload.trigger.attrs['aria-expanded'],'false');
docs.showPopover(); document.emit('pointerdown',{target:docLink});
assert.equal(docs.matches(),true);
document.emit('pointerdown',{target:outside}); assert.equal(docs.matches(),false);
assert.equal(dialog.hidden,false);
""")

    def test_focusout_and_escape_preserve_keyboard_navigation_and_editor(self):
        self.run_js(r"""
upload.open=true; file.focus();
document.emit('focusout',{target:file,relatedTarget:inner}); assert.equal(upload.open,true);
document.emit('focusout',{target:inner,relatedTarget:outside}); assert.equal(upload.open,false);
docs.showPopover(); docLink.focus(); document.emit('keydown',{key:'Escape'});
assert.equal(docs.matches(),false); assert.equal(document.activeElement,docsTrigger);
upload.open=true; document.emit('focusout',{target:file,relatedTarget:null});
assert.equal(upload.open,true); // janela/seletor nativo não é outra área da página
outside.focus(); assert.equal(upload.open,false);
assert.equal(dialog.hidden,false);
""")

    def test_opening_another_popover_closes_previous_across_implementations(self):
        self.run_js(r"""
for (let cycle=0; cycle<4; cycle++) {
  upload.open=true; docs.showPopover(); assert.equal(upload.open,false);
  codes.showPopover(); assert.equal(docs.matches(),false);
  upload.open=true; assert.equal(codes.matches(),false);
  context.hidden=false;
  document.emit('platform:popover-open',{detail:{panel:context}});
  assert.equal(upload.open,false);
  contextAction.focus(); assert.equal(context.hidden,false);
  docs.showPopover(); assert.equal(context.hidden,true);
  outside.focus(); assert.equal(docs.matches(),false);
}
assert.equal(document.listeners.focusout.length,1);
assert.equal(document.listeners.keydown.length,1);
""")

    def test_file_picker_keeps_upload_open_until_change_or_cancel(self):
        self.run_js(r"""
for (const end of ['change','cancel']) {
  upload.open=true; file.emit('click');
  document.emit('focusout',{target:file,relatedTarget:outside});
  document.emit('keydown',{key:'Escape'});
  assert.equal(upload.open,true);
  file.emit(end); file.focus(); assert.equal(upload.open,true);
  outside.focus(); assert.equal(upload.open,false);
}
""")

    def test_blur_without_destination_closes_only_when_page_retains_focus(self):
        self.run_js(r"""
(async () => {
  upload.open=true; file.focus();
  document.emit('focusout',{target:file,relatedTarget:null});
  document.activeElement=outside;
  await Promise.resolve(); assert.equal(upload.open,false);
  upload.open=true; file.focus(); document.hasFocus=()=>false;
  document.emit('focusout',{target:file,relatedTarget:null});
  document.activeElement=outside;
  await Promise.resolve(); assert.equal(upload.open,true);
  document.hasFocus=()=>true; file.emit('click');
  document.emit('focusout',{target:file,relatedTarget:null});
  await Promise.resolve(); assert.equal(upload.open,true);
})().catch(error=>{ console.error(error); process.exitCode=1; });
""")

    def test_opt_in_templates_do_not_register_dialogs_or_permanent_sidebar(self):
        from html.parser import HTMLParser

        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.registered = []
            def handle_starttag(self, tag, attrs):
                if "data-temporary-popover" in dict(attrs):
                    self.registered.append((tag, dict(attrs)))

        for template, count in (("_qualitative_page_intro.html", 1), ("qualitative_reader.html", 3),
                                ("_qualitative_records.html", 2), ("_header.html", 0),
                                ("_analysis_actions.html", 0)):
            tags = Tags()
            tags.feed((ROOT / "templates/platform" / template).read_text(encoding="utf-8"))
            self.assertEqual(len(tags.registered), count)
            self.assertTrue(all(tag != "dialog" for tag, _ in tags.registered))
        self.assertNotIn("mouseleave", SOURCE)
