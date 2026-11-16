/* Offline renderer regressions. Install locked dev dependencies with npm ci. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.JSDOM_MODULE || 'jsdom');
const web = path.resolve(__dirname, '../src/agent_library/web');
const dom = new JSDOM('<!doctype html><div id="result"></div>', {url:'http://127.0.0.1:8769/',runScripts:'outside-only'});
const w=dom.window, doc=w.document, output=doc.getElementById('result');
for(const file of ['vendor/markdown-it.min.js','vendor/purify.min.js','vendor/highlight.min.js','markdown.js']) w.eval(fs.readFileSync(path.join(web,file),'utf8'));
let passed=0;
function test(name,fn){fn();passed++;console.log('PASS',name)}
function render(text,options={}){w.ArchiveMarkdown.render(output,text,options);return output}

test('headings, inline emphasis, list, quote and horizontal rule',()=>{
  render('# 主标题\n\n## 小标题\n\n**粗体**与*斜体*，以及`内联代码`与~~删去~~。\n\n- 第一项\n  - 子项\n\n> 引用\n\n---');
  for(const tag of ['h1','h2','strong','em','code','s','ul ul','blockquote','hr'])assert.ok(output.querySelector(tag),tag);
});
test('GFM table alignment and a horizontal-scroll wrapper',()=>{
  render('| 名称 | 数值 |\n|:---|---:|\n| 中文 | 42 |');
  assert.equal(output.querySelectorAll('.md-table-wrap table tbody td').length,2);
  assert.equal(output.querySelector('th:last-child').dataset.mdAlign,'right');
});
test('task list is disabled and has a checked state',()=>{
  render('- [x] 已完成\n- [ ] 未完成');
  const checks=[...output.querySelectorAll('input')];assert.equal(checks.length,2);assert.ok(checks.every(x=>x.disabled));assert.ok(checks[0].checked);assert.ok(!checks[1].checked);
});
test('fenced source remains literal, highlighted, and code lines have provenance',()=>{
  render('开始\n\n```python\nprint("<script>no execution</script>")\n```',{trackSourceLines:true});
  const block=output.querySelector('.md-code-block');assert.equal(block.dataset.mdLine,'2');
  assert.equal(block.querySelector('code').textContent,'print("<script>no execution</script>")\n');
  assert.ok(block.querySelector('.hljs-string'));assert.equal(output.querySelectorAll('script').length,0);
});
test('indented code is a normal code block and source positions are tracked',()=>{
  render('标题\n\n    const x = 1;\n',{trackSourceLines:true});
  assert.equal(output.querySelector('.md-code-block').dataset.mdLine,'2');
  assert.equal(output.querySelector('code').textContent,'const x = 1;\n');
});
test('raw HTML, forms, scripts and SVG never become executable DOM',()=>{
  render('<img src="https://example.com/x" onerror="alert(1)">\n\n<svg><script>alert(1)</script></svg>\n\n<form><input autofocus></form>');
  assert.equal(output.querySelectorAll('img,script,svg,form,input,iframe,object,embed').length,0);
  assert.ok(output.textContent.includes('<img'));
});
test('javascript, encoded javascript, data and file links remain inactive',()=>{
  for(const href of ['javascript:alert(1)','&#x6a;avascript:alert(1)','data:text/html,hello','file:///etc/passwd']){
    render('[unsafe]('+href+')');assert.equal(output.querySelectorAll('a').length,0,href);
  }
});
test('safe external links have independent noopener tabs',()=>{
  render('[官方网站](https://example.com/path?a=1&b=2)');const a=output.querySelector('a');assert.ok(a);assert.equal(a.target,'_blank');assert.equal(a.rel,'noopener noreferrer');
});
test('historical remote images never create a remote request',()=>{
  render('![历史图片](https://private.example.com/tracking.png)');assert.equal(output.querySelectorAll('img').length,0);assert.ok(output.querySelector('.md-image-missing'));
});
test('even a resolver cannot introduce remote, data, protocol-relative or escaping image URLs',()=>{
  for(const src of ['https://evil.example/x','//evil.example/x','data:image/png;base64,AAAA','/api/media/../file']){
    render('![图片](image.png)',{resolveImage:()=>src});assert.equal(output.querySelectorAll('img').length,0,src);
  }
});
test('a preserved image can only use the controlled media endpoint',()=>{
  let settled=0;render('![保留的图片](old.png)',{resolveImage:()=>'/api/media/test_asset',onImageSettled:()=>settled++});
  const img=output.querySelector('img');assert.ok(img);assert.equal(img.getAttribute('src'),'/api/media/test_asset');assert.equal(img.alt,'保留的图片');img.dispatchEvent(new w.Event('load'));assert.equal(settled,1);
});
test('local image failures give a readable placeholder and notify layout',()=>{
  let settled=0;render('![失效的图片](old.png)',{resolveImage:()=>'/api/media/missing',onImageSettled:()=>settled++});
  output.querySelector('img').dispatchEvent(new w.Event('error'));assert.ok(output.querySelector('.md-image-missing'));assert.equal(settled,1);
});
test('query highlighting changes text nodes only and preserves code exactly',()=>{
  render('## 中文查询\n\n[中文链接](https://example.com/中文)\n\n```python\nprint("中文")\n```',{query:'中文'});
  assert.ok(output.querySelectorAll('mark.md-highlight').length>=3);assert.equal(output.querySelector('code').textContent,'print("中文")\n');assert.ok(output.querySelector('a').href.includes('example.com'));
});
test('large declared code stays complete without expensive syntax highlighting',()=>{
  const body='const value = "中文";\n'.repeat(8000);render('```javascript\n'+body+'```');assert.equal(output.querySelector('code').textContent,body);assert.equal(output.querySelectorAll('code .hljs-keyword').length,0);
});

(async()=>{
  let clipboard='';Object.defineProperty(w.navigator,'clipboard',{configurable:true,value:{writeText:async text=>{clipboard=text}}});
  render('```js\nconst 文本 = "<> & \\"";\n```',{query:'文本'});const exact=output.querySelector('code').textContent;
  output.querySelector('.md-copy-code').click();await new Promise(resolve=>setTimeout(resolve,0));assert.equal(clipboard,exact);passed++;console.log('PASS code copy preserves original characters and newlines');
  Object.defineProperty(w.navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('permission')}}});
  render('```text\n手动复制\n```');output.querySelector('.md-copy-code').click();await new Promise(resolve=>setTimeout(resolve,0));
  const fallback=output.querySelector('.md-copy-fallback');assert.ok(fallback);assert.equal(fallback.value,'手动复制\n');assert.equal(fallback.previousElementSibling.className,'md-code-toolbar');passed++;console.log('PASS unavailable clipboard provides visible manual-copy text');
  console.log('All',passed,'Markdown tests passed.');w.close();
})().catch(e=>{console.error(e);process.exitCode=1;w.close()});
