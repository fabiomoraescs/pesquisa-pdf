"""Paleta, Explorer, Margem e PDF atualizam por code_id sem reload."""

from pathlib import Path
import shutil
import subprocess
import unittest

from test_qualitative_records_frontend import DOM as RECORDS_DOM, SOURCE as RECORDS_SOURCE
from test_qualitative_viewer_frontend import SOURCE as VIEWER_SOURCE


ROOT = Path(__file__).resolve().parents[1]


class QualitativeCodeColorFrontendTests(unittest.TestCase):
    def run_node(self, source):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run([node, "-e", source], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_context_menus_are_slightly_larger_than_tags_and_keep_touch_targets(self):
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        tag = css.split(".platform-qualitative-margin-card {", 1)[1].split("}", 1)[0]
        menu = css.split(".platform-qualitative-context-menu {", 1)[1].split("}", 1)[0]
        button = css.split(".platform-qualitative-context-menu button {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: .73rem", tag)
        self.assertIn("font-size: .82rem", menu)
        self.assertIn("line-height: 1.25", menu)
        self.assertIn("font: inherit", button)
        self.assertIn("min-height: 2.25rem", button)
        self.assertIn(".platform-qualitative-context-menu button:hover", css)
        self.assertIn(".platform-qualitative-context-menu button:focus-visible", css)

    def test_tag_click_navigates_excerpt_for_editor_and_read_only_viewer(self):
        source = VIEWER_SOURCE[VIEWER_SOURCE.index("  marginTrack?.addEventListener('click'"):
                               VIEWER_SOURCE.index("  if (codeMenu && marginTrack) {")]
        self.run_node(r"""
const assert=require('node:assert/strict');
let handler,activated=0;
const marginTrack={addEventListener(_,callback){handler=callback;}};
const root={dataset:{canAnnotate:'1'}};
const card={dataset:{pageNumber:'1',excerptId:'excerpt'}},tag={dataset:{codeId:'A'}};
const event={target:{closest(selector){return selector==='[data-excerpt-id]'?card:
  selector==='.platform-qualitative-coding-tag[data-code-id]'?tag:null;}}};
const activateExcerpt=()=>activated++;
""" + source + r"""
handler(event);assert.equal(activated,1);
root.dataset.canAnnotate='0';handler(event);assert.equal(activated,2);
""")

    def test_color_patch_updates_explorer_repeatedly_and_error_never_changes_local_color(self):
        self.assertNotIn("location.reload", RECORDS_SOURCE)
        self.run_node(RECORDS_DOM + RECORDS_SOURCE + r"""
(async()=>{
  const colored=[],errors=[];
  document.addEventListener('qualitative:code-colored',event=>colored.push(event.detail));
  document.addEventListener('qualitative:color-code-error',event=>errors.push(event.detail.message));
  const change=(codeId,color)=>document.dispatchEvent(new CustomEvent('qualitative:color-code-requested',
    {detail:{codeId,color}}));
  for(const [index,color] of ['yellow','green','red','purple','blue'].entries()){
    change('A',color);assert.equal(requests.length,index+1);
    assert.deepEqual(JSON.parse(requests[index].options.body),{color});
    assert.equal(requests[index].options.headers['X-CSRFToken'],'csrf-test');
    await flush();assert.equal(initial.codes[0].color,color);
    assert.equal(initial.codes[1].color,'blue');
    assert.deepEqual(colored[index],{codeId:'A',color,color_hex:colorHex[color],
      color_text:'#000000'});
    const row=elements['[data-record-list="code"]'].children[0];
    const label=row.querySelector('[data-record-label], .platform-qualitative-record-label');
    assert.equal(label.style.values['--qualitative-code-color'],colorHex[color]);
    assert.equal(label.classes.has('has-code-color'),true);
  }
  failure=true;change('B','yellow');await flush();
  assert.equal(initial.codes[1].color,'blue');assert.deepEqual(errors,['Falha de teste']);
  change('B','yellow');await flush();assert.equal(initial.codes[1].color,'yellow');
  assert.equal(document.listeners['qualitative:color-code-requested'].length,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_ten_margin_tags_and_pdf_layers_update_only_matching_code(self):
        overlays = VIEWER_SOURCE[VIEWER_SOURCE.index("  const renderExcerptOverlay ="):
                                 VIEWER_SOURCE.index("  const scrollToExcerpt =")]
        margin = VIEWER_SOURCE[VIEWER_SOURCE.index("  const renderMargin ="):
                               VIEWER_SOURCE.index("  marginTrack?.addEventListener")]
        listener = VIEWER_SOURCE[VIEWER_SOURCE.index("  document.addEventListener('qualitative:code-colored'"):
                                 VIEWER_SOURCE.index("  document.addEventListener('qualitative:rename-code-error'")]
        self.run_node(r"""
const assert=require('node:assert/strict');
const make=tag=>({tag,children:[],style:{setProperty(name,value){this[name]=value;}},dataset:{},attrs:{},
  classList:{toggle(){}},append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},
  setAttribute(name,value){this.attrs[name]=value;}});
const document=new EventTarget();document.createElement=make;
const window={matchMedia:()=>({matches:true})};
const marginTrack=make('div'),margin={style:{}},marginViewport={style:{}},scroll={};
const root={dataset:{canAnnotate:'1'}},removingCodings=new Set(),activeExcerptId=null,generation=1;
let margins=0;const scheduleMargin=()=>margins++,syncMarginScroll=()=>{};
const exactItems=()=>[{bbox:[0,2,3,4]}],mergeGeometry=items=>items;
const placeBox=()=>make('box');
const excerpts=Array.from({length:10},(_,index)=>({id:'excerpt-'+index,start:index,end:index+1,
  page_hash:'hash',memo_count:0,codes:[{id:'A',name:'Raça',color:'blue',color_hex:'#93C5FD',
    color_text:'#000000',coding_id:'coding-'+index,origin:'manual'}]}));
excerpts[0].codes.push({id:'B',name:'Outro',color:'green',color_hex:'#86D8A8',
  color_text:'#000000',coding_id:'coding-b',origin:'manual'});
const surface={offsetTop:0,clientHeight:100,querySelector:()=>null,querySelectorAll:()=>[],
  append(item){this.latest=item;}};
const page={rendered:1,layout:{layout_available:true,page_text_hash:'hash',height:100},
  shell:{offsetTop:0},surface,excerpts};
const nodes=new Map([[1,page]]);
""" + overlays + margin + listener + r"""
renderMargin();renderExcerptOverlay(page);
assert.equal(marginTrack.children.length,10);
assert.equal(marginTrack.children[0].children[0].style['--qualitative-code-color'],'#93C5FD');
assert.equal(marginTrack.children[0].children[1].style['--qualitative-code-color'],'#86D8A8');
assert.equal(surface.latest.children.length,11);
document.dispatchEvent(new CustomEvent('qualitative:code-colored',{detail:{codeId:'A',color:'yellow',
  color_hex:'#FDE68A',color_text:'#000000'}}));
renderMargin();
assert.equal(margins,1);
assert.equal(excerpts.filter(item=>item.codes[0].color==='yellow').length,10);
assert.equal(excerpts[0].codes[1].color,'green');
assert.equal(marginTrack.children.filter(item=>item.children[0].style['--qualitative-code-color']==='#FDE68A').length,10);
assert.equal(surface.latest.children.filter(item=>item.dataset.codeId==='A' &&
  item.style['--qualitative-code-color']==='#FDE68A').length,10);
assert.equal(surface.latest.children.filter(item=>item.dataset.codeId==='B' &&
  item.style['--qualitative-code-color']==='#86D8A8').length,1);
assert.match(surface.latest.children[0].style.clipPath,/inset\(/);
""")

    def test_existing_context_menu_palette_marks_current_color_and_recovers_after_error(self):
        template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        self.assertIn("data-code-rename>Renomear código", template)
        self.assertIn("data-code-color-open>Alterar cor", template)
        self.assertIn("data-code-palette", template)
        code = VIEWER_SOURCE[VIEWER_SOURCE.index("  if (codeMenu && marginTrack) {"):
                             VIEWER_SOURCE.index("  const refreshPageExcerpts =")]
        self.run_node(r"""
const assert=require('node:assert/strict');
const document=new EventTarget(),marginTrack=new EventTarget();
const root={dataset:{canAnnotate:'1'}};
const actions={hidden:false},palette={hidden:true};
const rename={focus(){this.focused=true;},closest:s=>s==='[data-code-rename]'?rename:null};
const colorOpen={closest:s=>s==='[data-code-color-open]'?colorOpen:null};
const swatches=['blue','yellow','green','red','purple'].map(color=>({dataset:{codeColor:color},
  attrs:{},disabled:false,focus(){this.focused=true;},
  setAttribute(name,value){this.attrs[name]=value;},
  closest(selector){return selector==='[data-code-color]'?this:null;}}));
const codeMenu=new EventTarget();codeMenu.hidden=true;codeMenu.style={};
codeMenu.querySelector=selector=>({'[data-code-rename]':rename,
  '[data-code-menu-actions]':actions,'[data-code-palette]':palette})[selector];
codeMenu.querySelectorAll=()=>swatches;
Object.defineProperty(codeMenu,'offsetWidth',{get:()=>palette.hidden?180:220});
Object.defineProperty(codeMenu,'offsetHeight',{get:()=>palette.hidden?70:260});
const records={codes:[{id:'A',color:'blue'},{id:'B',color:'green'}]};
const innerWidth=390,innerHeight=700;let closed=0;const closeContextMenu=()=>closed++;
const errors=[];const noticeContext=message=>errors.push(message);
""" + code + r"""
const requests=[];document.addEventListener('qualitative:color-code-requested',event=>requests.push(event.detail));
const label=id=>({dataset:{codeId:id},closest(selector){
  return selector==='.platform-qualitative-coding-tag[data-code-id]'?this:null;},
  getBoundingClientRect(){return {left:35,bottom:80};}});
const open=id=>{const labelNode=label(id);
  const event=new Event('contextmenu',{cancelable:true});Object.defineProperty(event,'target',{value:labelNode});
  event.clientX=380;event.clientY=690;marginTrack.dispatchEvent(event);
  assert.equal(event.defaultPrevented,true);assert.equal(codeMenu.hidden,false);};
const click=target=>{const event=new Event('click');Object.defineProperty(event,'target',{value:target});
  codeMenu.dispatchEvent(event);};
open('A');click(colorOpen);assert.equal(palette.hidden,false);assert.equal(actions.hidden,true);
assert.equal(swatches[0].attrs['aria-checked'],'true');assert.equal(swatches[0].focused,true);
assert.equal(parseInt(codeMenu.style.left,10)<=162,true);
assert.equal(parseInt(codeMenu.style.top,10)<=432,true);
click(swatches[1]);assert.deepEqual(requests[0],{codeId:'A',color:'yellow'});
assert.equal(swatches.every(item=>item.disabled),true);
document.dispatchEvent(new CustomEvent('qualitative:color-code-error',
  {detail:{message:'Falha de teste'}}));
assert.equal(swatches.every(item=>!item.disabled),true);
assert.equal(codeMenu.hidden,false);assert.deepEqual(errors,['Falha de teste']);
click(swatches[1]);records.codes[0].color='yellow';
document.dispatchEvent(new CustomEvent('qualitative:code-colored',
  {detail:{codeId:'A',color:'yellow'}}));
assert.equal(codeMenu.hidden,true);
for(const color of ['green','purple','blue']){
  open('A');click(colorOpen);
  const swatch=swatches.find(item=>item.dataset.codeColor===color);click(swatch);
  records.codes[0].color=color;
  document.dispatchEvent(new CustomEvent('qualitative:code-colored',{detail:{codeId:'A',color}}));
  assert.equal(codeMenu.hidden,true);
}
open('B');click(colorOpen);assert.equal(swatches[2].attrs['aria-checked'],'true');
click(swatches[3]);assert.deepEqual(requests.at(-1),{codeId:'B',color:'red'});
document.dispatchEvent(new CustomEvent('qualitative:code-colored',{detail:{codeId:'B',color:'red'}}));
for(const area of ['text','left-padding','right-padding','empty-area']){
  const target=label('A');target.area=area;
  const event=new Event('click');Object.defineProperty(event,'target',{value:target});
  event.clientX=20;event.clientY=60;marginTrack.dispatchEvent(event);
  assert.equal(codeMenu.hidden,true);
  assert.equal(event.cancelBubble,false);
}
const remove={closest(selector){return selector==='[data-remove-coding]'?this:label('A');}};
const event=new Event('click');Object.defineProperty(event,'target',{value:remove});
marginTrack.dispatchEvent(event);assert.equal(codeMenu.hidden,true);
""")


if __name__ == "__main__":
    unittest.main()
