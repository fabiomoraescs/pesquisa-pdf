"""Interação local do painel, sem endpoint ou falsa resposta livre."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/platform_assistant.js").read_text(encoding="utf-8")


class AssistantFrontendTests(unittest.TestCase):
    def run_node(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run([node, "-e", script], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_open_questions_second_question_close_escape_and_focus(self):
        self.run_node(r"""
const assert=require('node:assert/strict');
const vm=require('node:vm');
let focused=null;
class Element {
  constructor() { this.listeners={}; this.children=[]; this.attrs={}; this.dataset={};
    this.classList={values:new Set(),toggle:(name,on)=>on?this.classList.values.add(name):this.classList.values.delete(name)}; }
  addEventListener(type,listener) { this.listeners[type]=listener; }
  fire(type,event={}) { this.listeners[type](event); }
  querySelector(selector) { return this.matches[selector]; }
  setAttribute(name,value) { this.attrs[name]=value; }
  focus() { focused=this; }
  append(child) { this.children.push(child); }
  contains(target) { return target===question0 || target===question1; }
}
const avatar=new Element(),panel=new Element(),closeButton=new Element(); panel.hidden=true;
const messages=new Element(),body=new Element(),payload=new Element(),assistant=new Element();
body.scrollHeight=500;
const question0=new Element(),question1=new Element();
question0.dataset.assistantQuestion='0';question1.dataset.assistantQuestion='1';
question0.closest=question1.closest=selector=>selector==='[data-assistant-question]'?question0:null;
question1.closest=selector=>selector==='[data-assistant-question]'?question1:null;
const suggestions=Array.from({length:5},(_,i)=>({question:'Pergunta '+i,answer:'Resposta '+i}));
payload.textContent=JSON.stringify({key:'qualitative',suggestions});
assistant.matches={'[data-assistant-toggle]':avatar,'[data-assistant-panel]':panel,
  '[data-assistant-close]':closeButton,'[data-assistant-messages]':messages,
  '[data-assistant-body]':body,'[data-assistant-payload]':payload};
const document={listeners:{},querySelector:()=>assistant,createElement:()=>new Element(),
  addEventListener(type,listener){this.listeners[type]=listener;}};
vm.runInNewContext(SOURCE,{document});
assert.equal(panel.hidden,true);avatar.fire('click');assert.equal(panel.hidden,false);
assert.equal(avatar.attrs['aria-expanded'],'true');assert.equal(focused,closeButton);
assistant.fire('click',{target:question0});
assert.deepEqual(messages.children.map(item=>item.textContent),['Pergunta 0','Resposta 0']);
assert.equal(messages.children[0].className,'platform-assistant-message is-user');
assert.equal(messages.children[1].className,'platform-assistant-message is-assistant');
assistant.fire('click',{target:question1});
assert.deepEqual(messages.children.map(item=>item.textContent),
  ['Pergunta 0','Resposta 0','Pergunta 1','Resposta 1']);
assert.equal(body.scrollTop,500);
let prevented=false;document.listeners.keydown({key:'Escape',preventDefault(){prevented=true;}});
assert.equal(prevented,true);assert.equal(panel.hidden,true);
assert.equal(avatar.attrs['aria-expanded'],'false');assert.equal(focused,avatar);
avatar.fire('click');closeButton.fire('click');assert.equal(panel.hidden,true);
assert.equal(focused,avatar);
""".replace("SOURCE", repr(SOURCE)))

    def test_no_free_question_or_external_provider_and_responsive_rules_exist(self):
        self.assertNotIn("fetch(", SOURCE)
        self.assertNotIn("sessionStorage", SOURCE)
        self.assertNotIn("location.reload", SOURCE)
        template = (ROOT / "templates/platform/_assistant.html").read_text(encoding="utf-8")
        self.assertNotIn("<textarea", template)
        self.assertNotIn("<form", template)
        self.assertIn('role="dialog"', template)
        self.assertIn('role="log"', template)
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn(".platform-assistant-panel[hidden]", css)
        self.assertIn(".platform-qualitative-focus .platform-assistant", css)
        self.assertIn(".platform-assistant-avatar:focus-visible", css)
        self.assertIn(".platform-assistant { left: .75rem; right: .75rem; bottom: .75rem; }", css)
        self.assertIn(".platform-assistant-panel { width: 100%;", css)
        self.assertIn(".platform-qualitative-focus .platform-assistant { top: auto; right: .75rem; bottom: 6.5rem; }", css)


if __name__ == "__main__":
    unittest.main()
