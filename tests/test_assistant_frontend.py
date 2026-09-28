"""Perguntas sugeridas locais e pergunta livre via endpoint do Assistente."""

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

    def test_suggestions_remain_local_and_free_question_uses_json_post(self):
        self.run_node(r"""
const assert=require('node:assert/strict');
const vm=require('node:vm');
let focused=null;
class Element {
  constructor() { this.listeners={}; this.children=[]; this.attrs={}; this.dataset={};
    this.classList={values:new Set(),toggle:(name,on)=>on?this.classList.values.add(name):this.classList.values.delete(name)}; }
  addEventListener(type,listener) { this.listeners[type]=listener; }
  fire(type,event={}) { return this.listeners[type](event); }
  querySelector(selector) { return this.matches[selector]; }
  setAttribute(name,value) { this.attrs[name]=value; }
  focus() { focused=this; }
  append(child) { this.children.push(child); }
  contains(target) { return target===question0 || target===question1; }
}
const avatar=new Element(),panel=new Element(),closeButton=new Element(); panel.hidden=true;
const messages=new Element(),body=new Element(),payload=new Element(),assistant=new Element();
const form=new Element(),input=new Element(),sendButton=new Element(),error=new Element(),csrf=new Element();
form.action='/assistant/ask';csrf.value='csrf-token';error.hidden=true;
body.scrollHeight=500;
const question0=new Element(),question1=new Element();
question0.dataset.assistantQuestion='0';question1.dataset.assistantQuestion='1';
question0.closest=question1.closest=selector=>selector==='[data-assistant-question]'?question0:null;
question1.closest=selector=>selector==='[data-assistant-question]'?question1:null;
const suggestions=Array.from({length:5},(_,i)=>({question:'Pergunta '+i,answer:'Resposta '+i}));
payload.textContent=JSON.stringify({key:'qualitative',page:'qualitative.page',
  reference:{project_id:'safe-id'},suggestions});
assistant.matches={'[data-assistant-toggle]':avatar,'[data-assistant-panel]':panel,
  '[data-assistant-close]':closeButton,'[data-assistant-messages]':messages,
  '[data-assistant-body]':body,'[data-assistant-payload]':payload,
  '[data-assistant-form]':form,'[data-assistant-input]':input,
  '[data-assistant-send]':sendButton,'[data-assistant-error]':error,
  '[data-assistant-csrf]':csrf};
const document={listeners:{},querySelector:()=>assistant,createElement:()=>new Element(),
  addEventListener(type,listener){this.listeners[type]=listener;}};
let requests=[];
let nextResponse={ok:true,json:async()=>({answer:'Resposta provisória'})};
const fetch=(url,options)=>{requests.push({url,options});return Promise.resolve(nextResponse);};
vm.runInNewContext(SOURCE,{document,fetch});
assert.equal(panel.hidden,true);avatar.fire('click');assert.equal(panel.hidden,false);
assert.equal(avatar.attrs['aria-expanded'],'true');assert.equal(focused,closeButton);
assistant.fire('click',{target:question0});
assert.deepEqual(messages.children.map(item=>item.textContent),['Pergunta 0','Resposta 0']);
assert.equal(messages.children[0].className,'platform-assistant-message is-user');
assert.equal(messages.children[1].className,'platform-assistant-message is-assistant');
assistant.fire('click',{target:question1});
assert.deepEqual(messages.children.map(item=>item.textContent),
  ['Pergunta 0','Resposta 0','Pergunta 1','Resposta 1']);
assert.equal(requests.length,0);
assert.equal(body.scrollTop,500);
let prevented=false;document.listeners.keydown({key:'Escape',preventDefault(){prevented=true;}});
assert.equal(prevented,true);assert.equal(panel.hidden,true);
assert.equal(avatar.attrs['aria-expanded'],'false');assert.equal(focused,avatar);
avatar.fire('click');closeButton.fire('click');assert.equal(panel.hidden,true);
assert.equal(focused,avatar);
(async()=>{
  avatar.fire('click');
  input.value='  Como funciona?  ';
  let prevented=false;
  const sending=form.fire('submit',{preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.equal(messages.children.at(-1).textContent,'Como funciona?');
  assert.equal(input.value,'');
  assert.equal(input.disabled,true);assert.equal(sendButton.disabled,true);
  assert.equal(sendButton.textContent,'Enviando...');
  await form.fire('submit',{preventDefault(){}});
  assert.equal(requests.length,1);
  await sending;
  assert.equal(requests.length,1);assert.equal(requests[0].url,'/assistant/ask');
  assert.equal(requests[0].options.method,'POST');
  assert.equal(requests[0].options.credentials,'same-origin');
  assert.equal(requests[0].options.headers['Content-Type'],'application/json');
  assert.equal(requests[0].options.headers.Accept,'application/json');
  assert.equal(requests[0].options.headers['X-CSRFToken'],'csrf-token');
  const sent=JSON.parse(requests[0].options.body);
  assert.deepEqual(sent,{question:'Como funciona?',context:'qualitative',
    page:'qualitative.page',reference:{project_id:'safe-id'}});
  assert.equal(messages.children.at(-1).textContent,'Resposta provisória');
  assert.equal(input.disabled,false);assert.equal(sendButton.disabled,false);
  assert.equal(sendButton.textContent,'Enviar');assert.equal(focused,input);
  assert.equal(body.scrollTop,500);

  input.value='  ';await form.fire('submit',{preventDefault(){}});
  assert.equal(requests.length,1);assert.equal(error.hidden,false);
  input.value='a'.repeat(1001);await form.fire('submit',{preventDefault(){}});
  assert.equal(requests.length,1);
  nextResponse={ok:false,json:async()=>({error:'Erro controlado'})};
  input.value='Teste de erro';await form.fire('submit',{preventDefault(){}});
  assert.equal(error.textContent,'Erro controlado');
  assert.equal(input.disabled,false);assert.equal(sendButton.disabled,false);
  nextResponse={ok:true,json:async()=>({})};
  input.value='Resposta inválida';await form.fire('submit',{preventDefault(){}});
  assert.equal(error.textContent,'Resposta inválida do Assistente.');
  assert.equal(input.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SOURCE", repr(SOURCE)))

    def test_free_question_form_and_responsive_rules_exist(self):
        self.assertIn("fetch(form.action", SOURCE)
        self.assertNotIn("sessionStorage", SOURCE)
        self.assertNotIn("localStorage", SOURCE)
        self.assertNotIn("location.reload", SOURCE)
        template = (ROOT / "templates/platform/_assistant.html").read_text(encoding="utf-8")
        self.assertIn('data-assistant-form', template)
        self.assertIn('action="{{ url_for(\'assistant.ask\') }}"', template)
        self.assertIn('placeholder="Faça uma pergunta..."', template)
        self.assertIn('maxlength="1000"', template)
        self.assertIn('data-assistant-csrf', template)
        self.assertIn('data-assistant-send>Enviar</button>', template)
        self.assertIn('data-assistant-error role="alert"', template)
        self.assertIn('role="dialog"', template)
        self.assertIn('role="log"', template)
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn(".platform-assistant-panel[hidden]", css)
        self.assertIn(".platform-assistant-form { flex: none;", css)
        self.assertIn(".platform-assistant-body { flex: 1 1 auto;", css)
        self.assertIn(".platform-qualitative-focus .platform-assistant", css)
        self.assertIn(".platform-assistant-avatar:focus-visible", css)
        self.assertIn(".platform-assistant { left: .75rem; right: .75rem; bottom: .75rem; }", css)
        self.assertIn(".platform-assistant-panel { width: 100%;", css)
        self.assertIn(".platform-qualitative-focus .platform-assistant { top: auto; right: .75rem; bottom: 6.5rem; }", css)


if __name__ == "__main__":
    unittest.main()
