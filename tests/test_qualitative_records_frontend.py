"""Fluxo real de exclusão em Node, incluindo currentTarget nulo após await."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/qualitative_records.js").read_text(encoding="utf-8")
DOM = r"""
const assert = require('node:assert/strict');
class Element extends EventTarget {
  constructor(){super();this.fields=new Map();this.dataset={};this.children=[];this.listeners={};this.disabled=false;this.open=false;this.textContent='';
    this.style={values:{},setProperty(name,value){this.values[name]=value;}};
    this.classes=new Set();this.classList={add:name=>this.classes.add(name)};}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);super.addEventListener(name,fn);}
  querySelector(selector){return this.fields.get(selector);}
  querySelectorAll(){return [];}
  contains(){return true;}
  closest(selector){return this.selector===selector?this:null;}
  setAttribute(key,value){this[key]=value;}
  append(child){this.children.push(child);}
  replaceChildren(fragment){this.children=fragment.children;}
  showModal(){this.open=true;}
  close(){this.open=false;this.dispatchEvent(new Event('close'));}
  focus(){}
  select(){this.selected=true;}
  click(){if(!this.disabled)this.dispatchEvent(new Event('click'));}
}
const colorHex={blue:'#93C5FD',yellow:'#FDE68A',green:'#86D8A8',red:'#FCA5A5',purple:'#C4B5FD'};
const initial={codes:['A','B','C'].map(id=>({id,name:'Código '+id,excerpt_count:1,
  color:'blue',color_hex:colorHex.blue,color_text:'#000000'})),memos:[]};
const root=new Element(), elements={};
for(const selector of ['[data-records-initial]','[data-record-dialog]','[data-record-confirm-dialog]',
  '[data-record-row-template]','[data-record-csrf]','[data-record-error]','[data-record-confirm-error]',
  '[data-code-fields]','[data-code-description-field]','[data-memo-fields]','[name="name"]','[name="description"]','[name="text"]',
  '[data-record-form]','[data-record-dialog-title]','[data-record-confirm-title]','[data-record-confirm-message]',
  '[data-record-cancel]','[data-record-confirm-cancel]','[data-record-save]','[data-record-confirm-delete]',
  '[data-record-count="code"]','[data-record-empty="code"]','[data-record-list="code"]',
  '[data-record-count="memo"]','[data-record-empty="memo"]','[data-record-list="memo"]']) {
  root.fields.set(selector,elements[selector]=new Element());
}
elements['[data-records-initial]'].textContent=JSON.stringify(initial);
elements['[data-record-csrf]'].value='csrf-test';
root.dataset={codesUrl:'/base/codigos',memosUrl:'/base/memos'};
elements['[data-record-row-template]'].content={firstElementChild:{cloneNode(){
  const row=new Element();
  for(const [selector,action] of [['[data-record-label], .platform-qualitative-record-label',null],
    ['[data-record-context]',null],['[data-record-edit]','edit'],['[data-record-delete]','delete']]) {
    const item=new Element();item.selector=action?'[data-record-'+action+']':selector;row.fields.set(selector,item);
  }
  return row;
}}};
const document = new Element();
document.querySelector=selector=>selector==='[data-qualitative-records]'?root:selector==='[data-qualitative-viewer]'?{}:null;
document.createDocumentFragment=()=>new Element();
const updates=[];document.addEventListener('qualitative:records-updated',event=>updates.push(event.detail));
const requests=[];
let pending=[], failure=false, missing=false;
const fetch=(url,options)=>new Promise(resolve=>{
  requests.push({url,options});
  pending.push(()=>{
    if(missing){missing=false;resolve({ok:false,status:404,json:async()=>{throw new SyntaxError('HTML');}});return;}
    if(failure){failure=false;resolve({ok:false,json:async()=>({error:'Falha de teste'})});return;}
    const id=url.split('/').pop();
    if(options.method==='DELETE') initial.codes=initial.codes.filter(code=>code.id!==id);
    if(options.method==='PATCH') {
      const changes=JSON.parse(options.body),code=initial.codes.find(item=>item.id===id);
      Object.assign(code,changes);
      if(changes.color){code.color_hex=colorHex[changes.color];code.color_text='#000000';}
    }
    resolve({ok:true,json:async()=>JSON.parse(JSON.stringify(initial))});
  });
});
const flush=async()=>{pending.splice(0).forEach(fn=>fn());await new Promise(resolve=>setImmediate(resolve));};
const modal=elements['[data-record-confirm-dialog]'],button=elements['[data-record-confirm-delete]'];
const cancel=elements['[data-record-confirm-cancel]'],message=elements['[data-record-confirm-message]'];
const error=elements['[data-record-confirm-error]'];
const openDelete=id=>{
  const rendered=elements['[data-record-list="code"]'].children;
  const target=rendered.length?rendered.map(row=>row.querySelector('[data-record-delete]')).find(el=>el.dataset.recordId===id):new Element();
  assert.ok(target,'Código da lista reconstruída deve continuar acessível');
  target.selector='[data-record-delete]';target.dataset={recordDelete:'code',recordId:id};
  const event=new Event('click');Object.defineProperty(event,'target',{value:target});root.dispatchEvent(event);
  assert.equal(modal.open,true);assert.ok(message.textContent.includes('Código '+id));assert.equal(button.disabled,false);
};
"""


class QualitativeRecordsFrontendTests(unittest.TestCase):
    def run_js(self, scenario):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node não disponível')
        result = subprocess.run([node, '-e', DOM + SOURCE + '\n(async()=>{' + scenario
                                 + "})().catch(error=>{console.error(error);process.exitCode=1;});"],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_delete_a_b_c_reenables_button_and_updates_dependents_without_reload(self):
        self.assertNotIn('location.reload', SOURCE)
        self.run_js(r"""
for(const [index,id] of ['A','B','C'].entries()) {
  openDelete(id);button.click();button.click(); // mesmo clique duplo não duplica o request
  assert.equal(button.disabled,true);assert.equal(cancel.disabled,true);
  assert.equal(requests.length,index+1);
  await flush();
  assert.equal(modal.open,false);assert.equal(button.disabled,false);assert.equal(cancel.disabled,false);
  assert.equal(message.textContent,'');assert.equal(error.hidden,true);
  assert.equal(elements['[data-record-count="code"]'].textContent,`Códigos (${2-index})`);
  assert.equal(updates.length,index+1);
  assert.equal(elements['[data-record-list="code"]'].children.length,2-index);
}
assert.deepEqual(requests.map(r=>r.url),['/base/codigos/A','/base/codigos/B','/base/codigos/C']);
for(const {options} of requests){assert.equal(options.method,'DELETE');assert.equal(options.headers['X-CSRFToken'],'csrf-test');}
assert.equal(root.listeners.click.length,1);assert.equal(button.listeners.click.length,1);
button.click();await flush();assert.equal(requests.length,3); // não sobrou ID anterior
""")

    def test_cancel_a_then_delete_b_and_escape_clear_previous_selection(self):
        self.run_js(r"""
openDelete('A');cancel.click();assert.equal(modal.open,false);assert.equal(message.textContent,'');
button.click();assert.equal(requests.length,0);
openDelete('B');assert.equal(message.textContent.includes('Código A'),false);button.click();await flush();
assert.deepEqual(initial.codes.map(code=>code.id),['A','C']);
assert.equal(requests[0].url,'/base/codigos/B');
openDelete('A');const escape=new Event('cancel',{cancelable:true});modal.dispatchEvent(escape);
assert.equal(escape.defaultPrevented,false);modal.close();button.click();assert.equal(requests.length,1);
openDelete('C');button.click();await flush();assert.equal(requests[1].url,'/base/codigos/C');
""")

    def test_error_allows_retry_or_another_code_and_pending_request_cannot_be_abandoned(self):
        self.run_js(r"""
openDelete('A');failure=true;button.click();
const escape=new Event('cancel',{cancelable:true});modal.dispatchEvent(escape);
assert.equal(escape.defaultPrevented,true);cancel.click();assert.equal(modal.open,true);
await flush();assert.equal(modal.open,true);assert.equal(button.disabled,false);assert.equal(cancel.disabled,false);
assert.equal(error.hidden,false);assert.equal(error.textContent,'Falha de teste');
failure=true;button.click();await flush();assert.equal(requests.length,2); // pode repetir A
cancel.click();openDelete('B');assert.equal(error.hidden,true);assert.equal(error.textContent,'');
button.click();await flush();assert.equal(requests[2].url,'/base/codigos/B');
assert.deepEqual(initial.codes.map(code=>code.id),['A','C']);
""")

    def test_rename_from_margin_reuses_dialog_and_can_repeat_without_reload(self):
        self.assertNotIn('location.reload', SOURCE)
        self.run_js(r"""
const renamed=[];document.addEventListener('qualitative:code-renamed',event=>renamed.push(event.detail));
const rename=id=>document.dispatchEvent(new CustomEvent('qualitative:rename-code-requested',
  {detail:{codeId:id}}));
const editor=elements['[data-record-dialog]'],name=elements['[name="name"]'];
const form=elements['[data-record-form]'];
for(const [index,id] of ['A','B','C'].entries()) {
  rename(id);
  assert.equal(editor.open,true);
  assert.equal(name.value,'Código '+id);
  assert.equal(name.selected,true);
  assert.equal(elements['[data-code-description-field]'].hidden,true);
  assert.equal(elements['[data-record-dialog-title]'].textContent,'Renomear código');
  name.value='Novo '+id;
  form.dispatchEvent(new Event('submit',{cancelable:true}));
  await flush();
  assert.equal(editor.open,false);
  assert.equal(initial.codes[index].name,'Novo '+id);
  assert.deepEqual(renamed[index],{codeId:id,name:'Novo '+id});
  assert.equal(requests[index].options.method,'PATCH');
  assert.equal(requests[index].url,'/base/codigos/'+id);
  assert.deepEqual(JSON.parse(requests[index].options.body),{name:'Novo '+id});
}
rename('A');assert.equal(name.value,'Novo A');
elements['[data-record-cancel]'].click();assert.equal(editor.open,false);
assert.equal(requests.length,3);
rename('A');name.value='Último A';form.dispatchEvent(new Event('submit',{cancelable:true}));await flush();
assert.equal(initial.codes[0].name,'Último A');
assert.equal(requests.length,4);
assert.equal(document.listeners['qualitative:rename-code-requested'].length,1);
""")

    def test_rename_error_keeps_old_name_and_allows_another_code(self):
        self.run_js(r"""
const rename=id=>document.dispatchEvent(new CustomEvent('qualitative:rename-code-requested',
  {detail:{codeId:id}}));
const editor=elements['[data-record-dialog]'],name=elements['[name="name"]'];
const form=elements['[data-record-form]'],error=elements['[data-record-error]'];
rename('A');name.value='Nome em conflito';failure=true;
form.dispatchEvent(new Event('submit',{cancelable:true}));await flush();
assert.equal(editor.open,true);assert.equal(error.hidden,false);
assert.equal(error.textContent,'Falha de teste');
assert.equal(initial.codes[0].name,'Código A');
assert.equal(elements['[data-record-save]'].disabled,false);
elements['[data-record-cancel]'].click();
rename('B');assert.equal(name.value,'Código B');assert.equal(error.hidden,true);
name.value='Novo B';form.dispatchEvent(new Event('submit',{cancelable:true}));await flush();
assert.equal(initial.codes[0].name,'Código A');assert.equal(initial.codes[1].name,'Novo B');
""")

    def test_rename_deleted_elsewhere_shows_readable_error_and_recovers(self):
        self.run_js(r"""
const rename=id=>document.dispatchEvent(new CustomEvent('qualitative:rename-code-requested',
  {detail:{codeId:id}}));
const name=elements['[name="name"]'],form=elements['[data-record-form]'];
rename('A');name.value='Novo A';missing=true;
form.dispatchEvent(new Event('submit',{cancelable:true}));await flush();
assert.equal(elements['[data-record-error]'].textContent,'Este registro não está mais disponível.');
assert.equal(initial.codes[0].name,'Código A');
elements['[data-record-cancel]'].click();rename('B');name.value='Novo B';
form.dispatchEvent(new Event('submit',{cancelable:true}));await flush();
assert.equal(initial.codes[1].name,'Novo B');
""")
