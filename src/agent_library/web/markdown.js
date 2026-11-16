/* Offline Markdown rendering. Libraries and licenses are pinned in vendor/. */
(function (global) {
  'use strict';
  const ALLOWED_TAGS = ['p','br','h1','h2','h3','h4','h5','h6','strong','em','s','del','blockquote','ul','ol','li','hr','pre','code','table','thead','tbody','tr','th','td','a','span'];
  const ALLOWED_ATTR = ['class','href','title','start','data-md-line','data-md-align','data-image-source','data-image-alt','data-math-index'];
  const MAX_HIGHLIGHT_LENGTH = 100000;
  const MAX_MATH_LENGTH = 12000;
  const aliases = {js:'javascript',ts:'typescript',py:'python',sh:'bash',shell:'bash',zsh:'bash',yml:'yaml',html:'xml',htm:'xml',svg:'xml',md:'markdown',csharp:'csharp','c#':'csharp','c++':'cpp',rb:'ruby'};
  let parser;
  const translate = (key, fallback) => global.LibraryI18n ? global.LibraryI18n.t(key) : fallback;

  function element(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function escapedAt(text, position) {
    let slashes=0;
    for(let p=position-1;p>=0&&text[p]==='\\';p--)slashes++;
    return slashes%2===1;
  }

  function closingMath(text, start, close, singleDollar) {
    let braces=0,fallback=-1;
    for(let p=start;p<text.length;p++) {
      const ch=text[p];
      if(singleDollar&&(ch==='\n'||ch==='\r'))return -1;
      // Scan escaped pairs once; long backslash runs must stay linear time.
      if(ch==='\\'&&!text.startsWith(close,p)) {
        if(singleDollar&&/[\r\n]/.test(text[p+1]||''))return -1;
        p++;continue;
      }
      if(ch==='{'){braces++;continue;}
      if(ch==='}'){braces=Math.max(0,braces-1);continue;}
      if(!text.startsWith(close,p))continue;
      const valid=!(singleDollar&&(p===start||/\s/.test(text[p-1])||/\d/.test(text[p+1]||'')||text[p+1]==='$'));
      if(braces){if(valid)fallback=p;p+=close.length-1;continue;}
      if(!valid)return -1;
      return p;
    }
    // An unmatched TeX group is still recognizably a formula. Let KaTeX reject
    // it into a collapsed source fallback instead of spilling raw delimiters.
    return fallback;
  }

  function mathInline(state, silent) {
    const start=state.pos,src=state.src;
    let open,close,display=false;
    if(src.startsWith('$$',start)){open=close='$$';display=true;}
    else if(src[start]==='$'){open=close='$';}
    else if(src.startsWith('\\(',start)){open='\\(';close='\\)';}
    else if(src.startsWith('\\[',start)){open='\\[';close='\\]';display=true;}
    else return false;
    if(escapedAt(src,start))return false;
    const contentStart=start+open.length;
    if(open==='$'&&(!src[contentStart]||/\s/.test(src[contentStart])))return false;
    const end=closingMath(src,contentStart,close,open==='$');
    if(end<0)return false;
    const content=src.slice(contentStart,end);
    if(!content.trim())return false;
    // A currency amount followed by prose is not a mathematical expression.
    // Examples: "$20 and $30", "$5 each, then $x$" remain ordinary text.
    if(open==='$'&&/^\d/.test(content)&&!/\\/.test(content)&&/^\d[\d.,]*\s+[A-Za-z\u3400-\u9fff]{2,}/.test(content))return false;
    if(open==='$'&&/^\d/.test(content)&&/[A-Za-z\\]/.test(src[end+1]||'')&&!/[\\^_{}]/.test(content))return false;
    if(!silent){const token=state.push('archive_math_inline','span',0);token.content=content;token.meta={display};}
    state.pos=end+close.length;
    return true;
  }

  function mathBlock(state,startLine,endLine,silent) {
    if(state.sCount[startLine]-state.blkIndent>=4)return false;
    const start=state.bMarks[startLine]+state.tShift[startLine];
    const tail=state.src.slice(start);
    let open,close,includeEnvironment=false;
    if(tail.startsWith('$$'))open=close='$$';
    else if(tail.startsWith('\\[')){open='\\[';close='\\]';}
    else {
      const env=/^\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?|multline\*?)\}/.exec(tail);
      if(!env)return false;
      open=env[0];close='\\end{'+env[1]+'}';includeEnvironment=true;
    }
    const contentStart=start+open.length;
    const end=closingMath(state.src,contentStart,close,false);
    if(end<0)return false;
    const closeEnd=end+close.length;
    let nextLine=startLine;
    while(nextLine<endLine&&state.eMarks[nextLine]<closeEnd)nextLine++;
    if(nextLine>=endLine||state.src.slice(closeEnd,state.eMarks[nextLine]).trim())return false;
    const from=includeEnvironment?start:contentStart,to=includeEnvironment?closeEnd:end;
    const lines=[];
    for(let line=startLine;line<=nextLine;line++) {
      const lo=line===startLine?from:state.bMarks[line]+Math.max(0,state.tShift[line]);
      const hi=line===nextLine?to:state.eMarks[line];
      lines.push(state.src.slice(lo,hi));
    }
    const content=lines.join('\n');
    if(!content.trim())return false;
    if(silent)return true;
    const token=state.push('archive_math_block','span',0);
    token.block=true;token.content=content;token.map=[startLine,nextLine+1];token.meta={display:true};
    state.line=nextLine+1;
    return true;
  }

  function mathPlaceholder(tokens,index,options,env) {
    const token=tokens[index];
    if(!Array.isArray(env.archiveMath))env.archiveMath=[];
    const id=env.archiveMath.push({source:token.content,display:!!token.meta?.display})-1;
    const line=token.attrGet('data-md-line');
    return '<span class="md-math-token" data-math-index="'+id+'"'+(line===null?'':' data-md-line="'+line+'"')+'></span>'+(token.block?'\n':'');
  }

  function getParser() {
    if (parser) return parser;
    parser = global.markdownit({html:false, breaks:false, linkify:true, typographer:false});
    parser.inline.ruler.before('escape','archive_math_inline',mathInline);
    parser.block.ruler.before('fence','archive_math_block',mathBlock,{alt:['paragraph','reference','blockquote','list']});
    parser.renderer.rules.archive_math_inline=mathPlaceholder;
    parser.renderer.rules.archive_math_block=mathPlaceholder;
    parser.core.ruler.push('archive_block_metadata', function (state) {
      for (const token of state.tokens) {
        if (state.env.trackSourceLines && token.block && token.map && token.nesting !== -1) token.attrSet('data-md-line', String(token.map[0]));
        if (token.type === 'th_open' || token.type === 'td_open') {
          const style = token.attrGet('style') || '';
          const alignment = /^text-align:(left|center|right)$/.exec(style);
          if (alignment) token.attrSet('data-md-align', alignment[1]);
          if (token.attrs) token.attrs = token.attrs.filter(pair => pair[0] !== 'style');
        }
      }
    });
    parser.renderer.rules.image = function (tokens, index, options, env, renderer) {
      const token = tokens[index], esc = parser.utils.escapeHtml;
      const alt = renderer.renderInlineAsText(token.children || [], options, env);
      // No img tag or src is created from source text. A local-only resolver
      // decides whether this historical reference has a preserved image.
      return '<span class="md-image-token" data-image-source="' + esc(token.attrGet('src') || '') + '" data-image-alt="' + esc(alt) + '"></span>';
    };
    parser.renderer.rules.code_block = function (tokens, index) {
      const token = tokens[index];
      const line = token.attrGet('data-md-line');
      return '<pre' + (line === null ? '' : ' data-md-line="' + line + '"') + '><code>' + parser.utils.escapeHtml(token.content) + '</code></pre>\n';
    };
    return parser;
  }

  function safeLink(value) {
    const url = String(value || '').trim();
    if (/^\/api\/(?:file\?|media\/)/.test(url) && !url.startsWith('//')) {
      try {
        const parsed = new URL(url, location.origin);
        if (parsed.origin === location.origin && /^\/api\/(?:file$|media\/)/.test(parsed.pathname)) return {url:parsed.pathname + parsed.search, local:true};
      } catch (_) {}
      return null;
    }
    try {
      const parsed = new URL(url);
      if (['http:','https:','mailto:'].includes(parsed.protocol)) return {url:parsed.href, local:false};
    } catch (_) {}
    return null;
  }

  function safeMedia(value) {
    if (typeof value !== 'string' || !value.startsWith('/api/media/') || value.startsWith('//')) return null;
    try {
      const parsed = new URL(value, location.origin);
      if (parsed.origin === location.origin && parsed.pathname.startsWith('/api/media/')) return parsed.pathname + parsed.search;
    } catch (_) {}
    return null;
  }

  function decorateLinks(container) {
    for (const a of container.querySelectorAll('a')) {
      const safe = safeLink(a.getAttribute('href'));
      if (!safe) {
        const span = element('span','md-link-unavailable');
        span.title = translate('sourceUnavailable','Original link unavailable');
        span.append(...a.childNodes);
        a.replaceWith(span);
      } else {
        a.setAttribute('href', safe.url);
        if (safe.local) {a.removeAttribute('target'); a.setAttribute('download','');}
        else {a.setAttribute('target','_blank'); a.setAttribute('rel','noopener noreferrer');}
      }
    }
  }

  function decorateTables(container) {
    for (const table of container.querySelectorAll('table')) {
      const wrap = element('div','md-table-wrap');
      if (table.dataset.mdLine !== undefined) wrap.dataset.mdLine = table.dataset.mdLine;
      wrap.tabIndex = 0;
      wrap.setAttribute('role','region');
      wrap.setAttribute('aria-label',translate('tableLabel','Table, scroll horizontally'));
      table.before(wrap); wrap.append(table);
    }
  }

  function decorateTasks(container) {
    for (const li of container.querySelectorAll('li')) {
      const parent = li.firstElementChild && li.firstElementChild.tagName === 'P' ? li.firstElementChild : li;
      const first = [...parent.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
      if (!first) continue;
      const match = /^\[( |x|X)\]\s+/.exec(first.textContent);
      if (!match) continue;
      const check = element('input','md-task-checkbox');
      check.type='checkbox'; check.disabled=true; check.checked=match[1].toLowerCase()==='x';
      check.setAttribute('aria-label', check.checked ? translate('completedTask','Completed') : translate('pendingTask','Not completed'));
      first.textContent = first.textContent.slice(match[0].length);
      parent.insertBefore(check, first); li.classList.add('md-task-list-item');
      li.parentElement.classList.add('md-task-list');
    }
  }

  function codeLabel(code) {
    const name = [...code.classList].find(c => c.startsWith('language-'));
    return name ? name.slice(9).slice(0,50) : '';
  }

  function copyButton(raw) {
    const button = element('button','md-copy-code',translate('copyCode','Copy code'));
    button.type='button'; button.setAttribute('aria-label',translate('copyCode','Copy code'));
    button.addEventListener('click', async function () {
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) throw Error('Clipboard unavailable');
        await navigator.clipboard.writeText(raw);
        button.textContent=translate('codeCopied','Copied');
        clearTimeout(button._copyReset);
        button._copyReset=setTimeout(() => {button.textContent=translate('copyCode','Copy code');}, 2000);
      } catch (_) {
        const block = button.closest('.md-code-block');
        let textarea = block.querySelector('.md-copy-fallback');
        if (!textarea) {
          textarea=element('textarea','md-copy-fallback');textarea.readOnly=true;
          textarea.setAttribute('aria-label',translate('codeSource','Code source; select and copy'));
          block.querySelector('.md-code-toolbar').after(textarea);
        }
        textarea.value=raw; textarea.hidden=false; textarea.focus({preventScroll:true});textarea.select();
        button.textContent=translate('codeCopyFallback','Select and copy the code below');
      }
    });
    return button;
  }

  function decorateCode(container) {
    for (const pre of [...container.querySelectorAll('pre')]) {
      const code = pre.firstElementChild;
      if (!code || code.tagName !== 'CODE') continue;
      const raw=code.textContent, language=codeLabel(code), canonical=aliases[language.toLowerCase()] || language.toLowerCase();
      const wrap=element('div','md-code-block'),bar=element('div','md-code-toolbar');
      const line=pre.dataset.mdLine ?? code.dataset.mdLine;
      if (line !== undefined) wrap.dataset.mdLine=line;
      pre.removeAttribute('data-md-line');code.removeAttribute('data-md-line');
      bar.append(element('span','md-code-language',language || translate('code','Code')),copyButton(raw));
      pre.before(wrap);wrap.append(bar,pre);code.classList.add('hljs');
      if (canonical && raw.length <= MAX_HIGHLIGHT_LENGTH && global.hljs && global.hljs.getLanguage(canonical)) {
        try {
          const highlighted=global.hljs.highlight(raw,{language:canonical,ignoreIllegals:true}).value;
          code.replaceChildren(global.DOMPurify.sanitize(highlighted,{ALLOWED_TAGS:['span'],ALLOWED_ATTR:['class'],ALLOW_DATA_ATTR:false,RETURN_DOM_FRAGMENT:true}));
        } catch (_) {code.textContent=raw;}
      }
    }
  }

  function missingImage(alt) {
    const span=element('span','md-image-missing');
    span.append(element('span','md-image-missing-label',translate('missingImage','Original image not preserved')));
    if (alt) span.append(element('span','md-image-alt',alt));
    return span;
  }

  function decorateImages(container, options) {
    for (const token of [...container.querySelectorAll('.md-image-token')]) {
      const src=token.getAttribute('data-image-source') || '',alt=token.getAttribute('data-image-alt') || '';
      let local=null;
      try {local=safeMedia(options.resolveImage ? options.resolveImage(src) : null);} catch (_) {}
      if (!local) {token.replaceWith(missingImage(alt));continue;}
      const wrap=element('span','md-image-wrap'),img=element('img','md-image');
      img.alt=alt;img.loading='lazy';img.decoding='async';
      const settled = function () {
        if (container.isConnected && typeof options.onImageSettled === 'function') options.onImageSettled(img);
      };
      img.addEventListener('load',settled,{once:true});
      img.addEventListener('error',function () {wrap.replaceWith(missingImage(alt));settled();},{once:true});
      wrap.append(img);token.replaceWith(wrap);img.src=local;
      if (img.complete && img.naturalWidth) queueMicrotask(settled);
    }
  }

  function unavailableMath(source,display) {
    const details=element('details','md-math-unavailable'+(display?' md-math-display':''));
    details.append(element('summary','',translate('mathUnavailable','Formula unavailable. View source')));
    const pre=element('pre'),code=element('code','',source.length>20000?source.slice(0,20000)+'\n['+translate('longFormula','Full formula preserved in conversation source.')+']':source);
    pre.append(code);details.append(pre);
    return details;
  }

  function decorateMath(container,expressions) {
    for(const token of [...container.querySelectorAll('.md-math-token')]) {
      const index=Number(token.getAttribute('data-math-index'));
      const expression=Number.isInteger(index)&&index>=0?expressions[index]:null;
      if(!expression){token.remove();continue;}
      const source=expression.source,display=expression.display;
      let wrap=element('span',display?'md-math-display':'md-math-inline');
      try {
        if(!global.katex||source.length>MAX_MATH_LENGTH||/\\(?:href|url|includegraphics|htmlClass|htmlId|htmlStyle|htmlData|html@mathml)\b/.test(source))throw Error('Unsupported historical expression');
        // KaTeX constructs a detached DOM tree from TeX with trust disabled.
        // Sanitized historical HTML is never allowed to create MathML/SVG/style.
        global.katex.render(source,wrap,{displayMode:display,output:'htmlAndMathml',throwOnError:false,trust:false,strict:'ignore',maxExpand:1000,maxSize:20,macros:{}});
        if(wrap.querySelector('.katex-error,script,iframe,object,embed,img,a,input,form,foreignObject')||[...wrap.querySelectorAll('*')].some(n=>[...n.attributes].some(a=>/^on/i.test(a.name))))throw Error('Invalid historical expression');
      } catch(_) {wrap=unavailableMath(source,display);}
      if(token.dataset.mdLine!==undefined)wrap.dataset.mdLine=token.dataset.mdLine;
      token.replaceWith(wrap);
    }
  }

  function highlightQuery(container, query) {
    if (!query) return;
    query=String(query);
    const needle=query.toLocaleLowerCase();
    if (!needle) return;
    const walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT),nodes=[];
    let node,total=0;
    while ((node=walker.nextNode())) {
      if (!node.parentElement || node.parentElement.closest('button,textarea,mark,.md-code-toolbar,.katex,.md-math-unavailable')) continue;
      if (node.textContent.toLocaleLowerCase().includes(needle)) nodes.push(node);
    }
    for (const textNode of nodes) {
      const text=textNode.textContent,lower=text.toLocaleLowerCase(),frag=document.createDocumentFragment();
      let start=0,index=lower.indexOf(needle);
      while (index>=0 && total<400) {
        frag.append(document.createTextNode(text.slice(start,index)),element('mark','md-highlight',text.slice(index,index+query.length)));
        start=index+query.length;index=lower.indexOf(needle,start);total++;
      }
      frag.append(document.createTextNode(text.slice(start)));textNode.replaceWith(frag);
      if (total>=400) break;
    }
  }

  function render(container, text, options) {
    options=options || {};text=String(text ?? '');
    container.classList.add('markdown-body');
    if (!global.markdownit || !global.DOMPurify) {container.textContent=text;return container;}
    const env={trackSourceLines:!!options.trackSourceLines,archiveMath:[]};
    const html=getParser().render(text,env);
    const clean=global.DOMPurify.sanitize(html,{ALLOWED_TAGS,ALLOWED_ATTR,ALLOW_DATA_ATTR:false,FORBID_TAGS:['script','style','iframe','object','embed','form','svg','math','video','audio'],FORBID_ATTR:['style','id','name'],RETURN_DOM_FRAGMENT:true});
    container.replaceChildren(clean);
    decorateLinks(container);decorateTables(container);decorateTasks(container);decorateCode(container);decorateImages(container,options);decorateMath(container,env.archiveMath);
    highlightQuery(container,options.query || '');
    return container;
  }

  global.ArchiveMarkdown=Object.freeze({render,version:'1.0.0'});
})(window);
