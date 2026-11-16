/* Developer-only DOM checks; jsdom may be installed outside this archive. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require(process.env.JSDOM_MODULE||'jsdom');
const web=path.resolve(__dirname,'../src/agent_library/web');
const dom=new JSDOM('<!doctype html><div id="result"></div>',{url:'http://127.0.0.1:8771/',runScripts:'outside-only'});
const w=dom.window,output=w.document.querySelector('#result');
for(const file of ['vendor/markdown-it.min.js','vendor/purify.min.js','vendor/highlight.min.js','vendor/katex.min.js','markdown.js'])w.eval(fs.readFileSync(path.join(web,file),'utf8'));
let passed=0;
function test(name,fn){fn();passed++;console.log('PASS',name)}
function render(text,options={}){w.ArchiveMarkdown.render(output,text,options);return output}
function maths(n){assert.equal(output.querySelectorAll('.katex').length,n)}

test('inline dollars render fractions and powers, without visible delimiters',()=>{
  render(String.raw`正文 $x^2+\frac{1}{2}$ 继续。`);maths(1);
  assert.ok(output.querySelector('.md-math-inline .mfrac'));
  assert.ok(![...output.childNodes].filter(n=>n.nodeType===3).some(n=>n.textContent.includes('$')));
});
test('inline parentheses are recognized before Markdown escape handling',()=>{
  render(String.raw`比例 \(\alpha+\beta\) 与 \(x_i\)。`);maths(2);
});
test('multiline double dollars render aligned equations',()=>{
  render(String.raw`开始

$$
\begin{aligned}
a&=b+c\\
d&=e
\end{aligned}
$$

结尾`,{trackSourceLines:true});maths(1);
  assert.ok(output.querySelector('.md-math-display .katex-display'));
  assert.equal(output.querySelector('.md-math-display').dataset.mdLine,'2');
});
test('display brackets render matrices',()=>{
  render(String.raw`\[\begin{pmatrix}1&2\\3&4\end{pmatrix}\]`);maths(1);
  assert.ok(output.querySelector('.md-math-display'));
});
test('bare supported display environments render',()=>{
  render(String.raw`\begin{align}a&=b\\c&=d\end{align}`);maths(1);
});
test('display formulas inside quotes and lists preserve Markdown structure',()=>{
  render(String.raw`> $$
> \frac{a}{b}
> $$

- 项目

  \[
  x+y
  \]`);maths(2);
  assert.ok(output.querySelector('blockquote .katex'));
  assert.ok(output.querySelector('li .katex'));
});
test('display delimiters may occur inside prose',()=>{
  render(String.raw`前文 $$x+y$$ 后文`);maths(1);
});
test('fenced and inline code remain literal',()=>{
  render('`$x$` 和 `\\(x\\)`\n\n```latex\n$$x+y$$\n\\[z\\]\n```\n\n正常 $a+b$');maths(1);
  assert.ok(output.querySelector('.md-code-block code').textContent.includes('$$x+y$$'));
  assert.equal(output.querySelector('code').textContent,'$x$');
});
test('escaped dollars are visible literal characters',()=>{
  render(String.raw`\$x\$ 及 \$20。`);maths(0);assert.ok(output.textContent.includes('$x$'));
});
test('currency amounts do not capture following formulas',()=>{
  for(const text of [String.raw`价格 $20 and $30，变量 $x$。`,String.raw`cost $5 each, then $x$`,String.raw`成本 $20 / $30，x=$x$`,String.raw`费用 $20,$x$`]){
    render(text);maths(1);assert.ok(output.textContent.includes('$20')||output.textContent.includes('$5'));
  }
});
test('numeric mathematics with commands is preserved',()=>{
  render(String.raw`$2\pi r$，$10^{-3}$，$20+30$。`);maths(3);
});
test('escaped dollars inside text groups are not closing delimiters',()=>{
  render(String.raw`$\text{price \$5}+x$`);maths(1);assert.equal(output.querySelectorAll('.md-math-unavailable').length,0);
});
test('tables, headings and normal Markdown still render around mathematics',()=>{
  render(String.raw`## 指标

| 名称 | 公式 |
|---|---|
| 比例 | $\frac{a}{b}$ |

**结束**`);maths(1);assert.ok(output.querySelector('h2'));assert.ok(output.querySelector('table'));assert.ok(output.querySelector('strong'));
});
test('search highlighting does not mutate mathematical DOM',()=>{
  render(String.raw`x 及公式 $x^2$。`,{query:'x'});maths(1);
  assert.ok(output.querySelector('mark'));assert.equal(output.querySelectorAll('.katex mark').length,0);
});
test('invalid math is a collapsed source fallback',()=>{
  render(String.raw`$\frac{a}{b$`);
  assert.ok(output.querySelector('.md-math-unavailable'));
  render(String.raw`$\frac{a}$`);maths(0);
  const d=output.querySelector('.md-math-unavailable');assert.ok(d);assert.ok(!d.open);assert.ok(d.querySelector('code').textContent.includes('frac'));
});
test('unsafe TeX extensions never create links or network resources',()=>{
  for(const source of [String.raw`\href{javascript:alert(1)}{x}`,String.raw`\includegraphics{https://example.com/image.png}`,String.raw`\htmlStyle{background:url(https://example.com/x)}{x}`]){
    render('$'+source+'$');assert.ok(output.querySelector('.md-math-unavailable'));
    assert.equal(output.querySelectorAll('a,img,script,iframe,object,embed,form').length,0);
  }
});
test('historical HTML cannot forge a math placeholder or execute SVG',()=>{
  render('<span class="md-math-token" data-math-index="0">evil</span>\n\n<svg onload="alert(1)"></svg>\n\n$x$');maths(1);
  assert.equal(output.querySelectorAll('svg,script,iframe').length,0);
  assert.ok(output.textContent.includes('<span'));
});
test('macros do not leak between messages',()=>{
  render(String.raw`$\gdef\archiveprivate{7}\archiveprivate$`);maths(1);
  render(String.raw`$\archiveprivate$`);assert.ok(output.querySelector('.md-math-unavailable')||output.textContent.includes('archiveprivate'));
});
test('expansion loops and oversized math fail without hanging',()=>{
  const start=Date.now();render(String.raw`$\def\x{\x}\x$`);assert.ok(Date.now()-start<1500);assert.ok(output.querySelector('.md-math-unavailable'));
  render('$$'+ 'x+'.repeat(7000)+'x$$');assert.ok(output.querySelector('.md-math-unavailable'));
});
test('long backslash sequences are scanned in linear time',()=>{
  const start=Date.now();render('$'+ '\\'.repeat(50000)+'x$');assert.ok(Date.now()-start<1500);
});

console.log('All',passed,'math tests passed.');w.close();
