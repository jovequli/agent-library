(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const I = window.LibraryI18n;
  const t = key => I.t(key);
  const providerNames = {claude:'Claude',claude_code:'Claude Code',cowork:'Cowork'};
  const noteKeys = ['goal','constraints','decisions','completed','pending','files'];
  const state = {
    token:'', libraries:[], libraryId:'', sessions:[], providers:[], sessionTotal:0, session:null,
    messages:[], preview:[], hasMore:false, hasAfter:false, followLatest:true, before:null, loadingOlder:false,
    listEpoch:0, sessionEpoch:0, handoffEpoch:0, listLoading:false,
    handoff:null, handoffContext:null, notesDirty:false, previewMessage:null,
    activeJob:null, jobEpoch:0, activeJobKind:'', jobFinished:false, jobSnapshot:null,
    resourceSession:'', resourceSignature:'', attachmentsBuilt:false
  };
  let searchTimer, toastTimer, previewTimer;

  function node(tag, className, text) {
    const element=document.createElement(tag);
    if(className)element.className=className;
    if(text!==undefined)element.textContent=String(text);
    return element;
  }
  function stored(key) { try{return localStorage.getItem('agent-library.'+key)||'';}catch(_){return '';} }
  function remember(key,value) {try{localStorage.setItem('agent-library.'+key,value);}catch(_){} }
  function library() {return state.libraries.find(item=>item.id===state.libraryId);}
  function providerName(id) {return providerNames[id]||state.providers.find(item=>item.id===id)?.name||id||'';}
  function url(path,params={}) {
    const query=new URLSearchParams();
    for(const [key,value] of Object.entries(params))if(value!==undefined&&value!==null&&value!=='')query.set(key,String(value));
    return path+(query.size?'?'+query.toString():'');
  }
  async function api(path,{method='GET',body,signal}={}) {
    const headers={'Accept':'application/json'};
    const options={method,headers,credentials:'same-origin',signal};
    if(method!=='GET'){headers['X-Agent-Library-Token']=state.token;headers['Content-Type']='application/json';options.body=JSON.stringify(body??{});}
    const response=await fetch(path,options);
    let result;
    try{result=await response.json();}catch(_){throw new Error(t('requestFailed')+' ('+response.status+')');}
    if(!response.ok){const error=new Error(result.error||t('requestFailed'));error.code=result.code;throw error;}
    return result;
  }
  function errorText(error) {
    const codes={invalid_path:'invalidPath',library_not_found:'chooseLibrary'};
    return codes[error?.code]?t(codes[error.code]):String(error?.message||error||t('error'));
  }
  function toast(message,error=false) {
    clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;
    toastTimer=setTimeout(()=>{$('toast').hidden=true;},error?9000:4200);
  }
  function displayDate(value,full=false) {
    if(!value)return t('unknownDate');
    const date=new Date(value);
    if(!Number.isFinite(date.getTime()))return t('unknownDate');
    return new Intl.DateTimeFormat(I.language,full?{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}:{year:'numeric',month:'short',day:'numeric'}).format(date);
  }
  function monthLabel(value) {
    const date=new Date(value);
    return Number.isFinite(date.getTime())?new Intl.DateTimeFormat(I.language,{year:'numeric',month:'long'}).format(date):t('unknownDate');
  }
  function shortProject(value) {return String(value).replace(/[\\/]+$/,'').split(/[\\/]/).pop()||value;}
  function roleName(role) {return t(role==='user'?'you':role==='assistant'?'assistant':role==='tool'?'tool':'system');}
  function cleanPath(value) {const text=String(value||'').trim();return text.length>1&&((text[0]==='"'&&text.at(-1)==='"')||(text[0]==="'"&&text.at(-1)==="'"))?text.slice(1,-1):text;}
  function localURL(value,pathPrefix='/api/') {
    if(typeof value!=='string')return null;
    try{const parsed=new URL(value,location.origin);return parsed.origin===location.origin&&parsed.pathname.startsWith(pathPrefix)?parsed.pathname+parsed.search:null;}catch(_){return null;}
  }
  function openDialog(id){$('preview-card').hidden=true;const dialog=$(id);if(!dialog.open)dialog.showModal();}
  function closeDialog(id){$(id).close();}

  function renderLibraryOptions() {
    const select=$('library-select');select.replaceChildren();
    if(!state.libraries.length){const opt=node('option','',t('noLibrary'));opt.value='';select.append(opt);}
    for(const item of state.libraries){const opt=node('option','',item.name);opt.value=item.id;select.append(opt);}
    select.value=state.libraryId;
    select.disabled=!state.libraries.length;
    $('backup-submit').disabled=!state.libraryId;
    renderImportLibraryOptions();
  }
  function renderImportLibraryOptions() {
    const select=$('import-library'),old=select.value;
    select.replaceChildren();
    for(const item of state.libraries){const opt=node('option','',item.name);opt.value=item.id;select.append(opt);}
    const create=node('option','',t('createNewOption'));create.value='__new__';select.append(create);
    select.value=(old==='__new__'||state.libraries.some(item=>item.id===old))?old:(state.libraryId||'__new__');
    importDestinationChanged();
  }
  function importDestinationChanged() {
    const create=$('import-library').value==='__new__';
    $('import-name-label').hidden=!create;$('import-name').required=create;
  }
  async function refreshState(preferred) {
    const data=await api('/api/state');
    state.token=data.csrf_token||'';state.libraries=data.libraries||[];
    const desired=preferred||state.libraryId||stored('library');
    state.libraryId=state.libraries.some(item=>item.id===desired)?desired:(state.libraries[0]?.id||'');
    remember('library',state.libraryId);$('version').textContent=data.version?'v'+data.version:'';
    renderLibraryOptions();
  }
  function clearConversation() {
    state.sessionEpoch++;state.session=null;state.messages=[];state.preview=[];
    $('conversation').hidden=true;$('welcome').hidden=false;$('messages').replaceChildren();$('preview-rail').replaceChildren();
    $('preview-card').hidden=true;state.previewMessage=null;
    resetSessionResources();
    if($('handoff-dialog').open)$('handoff-dialog').close();
  }
  async function chooseLibrary(id) {
    state.libraryId=id;remember('library',id);clearConversation();
    $('search').value='';$('provider-filter').value='';$('project-filter').value='';$('all-records').checked=false;
    await loadSessions();
  }
  function sessionParams(offset=0) {
    return {library_id:state.libraryId,q:$('search').value.trim(),provider:$('provider-filter').value,project:$('project-filter').value,kind:$('all-records').checked?'all':'main',limit:50,offset};
  }
  async function loadSessions(append=false) {
    if(!state.libraryId){state.sessions=[];state.sessionTotal=0;renderSessions();renderProjects([]);renderProviders([]);return;}
    const epoch=++state.listEpoch;
    state.listLoading=true;$('session-list').setAttribute('aria-busy','true');$('more-sessions').disabled=true;
    if(!append){state.sessions=[];renderSessions(true);}
    try{
      const data=await api(url('/api/sessions',sessionParams(append?state.sessions.length:0)));
      if(epoch!==state.listEpoch)return;
      state.sessions=append?[...state.sessions,...(data.items||[])]:data.items||[];
      state.sessionTotal=Number(data.total)||0;renderProjects(data.projects||[]);renderProviders(data.providers||[]);renderSessions();
    }catch(error){if(epoch===state.listEpoch){renderSessions();toast(errorText(error),true);}}
    finally{if(epoch===state.listEpoch){state.listLoading=false;$('session-list').setAttribute('aria-busy','false');$('more-sessions').disabled=false;}}
  }
  function renderProjects(projects) {
    const select=$('project-filter'),selected=select.value;
    select.replaceChildren();const all=node('option','',t('allProjects'));all.value='';all.dataset.i18n='allProjects';select.append(all);
    for(const project of projects){const opt=node('option','',shortProject(project));opt.value=project;opt.title=project;select.append(opt);}
    if(selected&&!projects.includes(selected)){const opt=node('option','',shortProject(selected));opt.value=selected;select.append(opt);}
    select.value=selected;
  }
  function renderProviders(providers) {
    state.providers=providers;
    const select=$('provider-filter'),selected=select.value;
    const available=new Map(Object.entries(providerNames));
    for(const provider of providers){if(provider&&provider.id)available.set(provider.id,providerNames[provider.id]||provider.name||provider.id);}
    if(selected&&!available.has(selected))available.set(selected,selected);
    select.replaceChildren();const all=node('option','',t('allAgents'));all.value='';all.dataset.i18n='allAgents';select.append(all);
    for(const [id,name] of available){const option=node('option','',name);option.value=id;select.append(option);}
    select.value=selected;
  }
  function excerpt(value,max=180) {return String(value||'').replace(/\s+/g,' ').trim().slice(0,max);}
  function searchSnippet(container,text,query) {
    const clean=excerpt(text,220),index=clean.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
    if(!query||index<0){container.textContent=clean;return;}
    container.append(document.createTextNode(clean.slice(0,index)),node('mark','',clean.slice(index,index+query.length)),document.createTextNode(clean.slice(index+query.length)));
  }
  function renderSessions(loading=false) {
    const list=$('session-list');list.replaceChildren();
    $('session-count').textContent=state.sessionTotal?String(state.sessionTotal):'';
    $('more-sessions').hidden=loading||state.sessions.length>=state.sessionTotal;
    if(!state.sessions.length){list.append(node('p','list-empty',loading?t('loading'):$('search').value.trim()?t('noMatches'):state.libraryId?t('emptyLibrary'):t('noConversations')));return;}
    const groups=new Map();
    for(const session of state.sessions){const group=session.project?{key:'project:'+session.project,label:shortProject(session.project),title:session.project}:{key:'month:'+monthLabel(session.updated_at||session.created_at),label:monthLabel(session.updated_at||session.created_at)};if(!groups.has(group.key))groups.set(group.key,{...group,items:[]});groups.get(group.key).items.push(session);}
    for(const group of groups.values()){
      const heading=node('h2','list-group-title',group.label);if(group.title)heading.title=group.title;list.append(heading);
      for(const session of group.items){
        const item=node('button','session-item'+(state.session?.id===session.id?' active':''));item.type='button';item.dataset.sessionId=session.id;item.setAttribute('aria-current',state.session?.id===session.id?'true':'false');
        item.append(node('div','session-title',session.title||t('untitled')));
        const meta=node('div','session-info');meta.append(node('span','session-provider',providerName(session.provider)),node('span','',displayDate(session.updated_at||session.created_at)));
        if(session.kind&&session.kind!=='main')meta.append(node('span','',t(session.kind)));
        if(session.match_hidden)meta.append(node('span','',t('hiddenMatch')));
        item.append(meta);
        if($('search').value.trim()){
          const matched=session.matches?.[0];const preview=typeof matched==='object'?matched.text||matched.preview:matched;
          const snippet=node('p','session-snippet');searchSnippet(snippet,preview||session.preview||'', $('search').value.trim());if(snippet.textContent)item.append(snippet);
        }
        item.addEventListener('click',()=>{if(session.match_hidden)$('show-details').checked=true;openSession(session,session.match_ordinal??undefined);});list.append(item);
      }
    }
  }
  function renderConversationHeading() {
    const session=state.session;if(!session)return;
    $('conversation-title').textContent=session.title||t('untitled');$('conversation-provider').textContent=providerName(session.provider);
    $('conversation-date').textContent=displayDate(session.updated_at||session.created_at);
    $('conversation-kind').textContent=session.kind&&session.kind!=='main'?t(session.kind):'';
    const taskPaths=Array.isArray(session.task_paths)?session.task_paths.filter(path=>typeof path==='string'):[];
    $('conversation-project').hidden=!session.project;$('conversation-project').textContent=session.project?t('projectPrefix')+'  '+session.project:'';$('conversation-project').title=taskPaths.length?taskPaths.join('\n'):session.project||'';
    renderSessionResources();
  }
  function resetSessionResources() {
    state.resourceSession='';state.resourceSignature='';state.attachmentsBuilt=false;
    $('conversation-resources').hidden=true;$('session-attachments').hidden=true;$('session-attachments').open=false;
    $('session-attachment-gallery').replaceChildren();$('session-missing-media').hidden=true;
    $('session-evidence').hidden=true;$('session-evidence').open=false;$('session-evidence-content').replaceChildren();
  }
  function taskAttachments() {
    return (Array.isArray(state.session?.attachments)?state.session.attachments:[]).filter(item=>item&&localURL(item.url,'/api/media/')&&(item.mime||'').startsWith('image/'));
  }
  function buildTaskAttachments() {
    if(state.attachmentsBuilt||!$('session-attachments').open)return;
    state.attachmentsBuilt=true;const gallery=$('session-attachment-gallery');gallery.replaceChildren();
    for(const [index,item] of taskAttachments().entries()){
      const path=localURL(item.url,'/api/media/'),name=String(item.alt||item.name||t('preservedImage'));
      const button=node('button','task-image');button.type='button';button.dataset.attachmentIndex=String(index);button.setAttribute('aria-label',t('openImage')+': '+name);
      const image=node('img');image.loading='lazy';image.decoding='async';image.alt=name;image.src=path;
      button.append(image,node('span','task-image-caption',name));button.addEventListener('click',()=>showImage(path,name));gallery.append(button);
    }
  }
  function updateTaskAttachmentLabels() {
    const attachments=taskAttachments();
    for(const button of $('session-attachment-gallery').querySelectorAll('.task-image')){
      const item=attachments[Number(button.dataset.attachmentIndex)];if(!item)continue;
      const name=String(item.alt||item.name||t('preservedImage'));
      button.setAttribute('aria-label',t('openImage')+': '+name);button.querySelector('img').alt=name;button.querySelector('.task-image-caption').textContent=name;
    }
  }
  function renderSessionResources() {
    const session=state.session;if(!session){resetSessionResources();return;}
    if(state.resourceSession!==session.id){resetSessionResources();state.resourceSession=session.id;}
    const attachments=taskAttachments(),signature=attachments.map(item=>item.id+':'+item.url).join('|');
    if(signature!==state.resourceSignature){state.resourceSignature=signature;state.attachmentsBuilt=false;$('session-attachment-gallery').replaceChildren();}
    $('session-attachments').hidden=!attachments.length;
    $('session-attachments-label').textContent=t('taskAttachments')+' ('+attachments.length+')';
    $('session-attachments-note').textContent=t('taskAttachmentsNote');
    const missing=Array.isArray(session.missing_media)?session.missing_media:[];
    $('session-missing-media').hidden=!missing.length;
    $('session-missing-media').textContent=missing.length?t('missingTaskImages')+' ('+missing.length+')':'';
    const evidence=$('session-evidence-content');evidence.replaceChildren();
    const paths=Array.isArray(session.task_paths)?session.task_paths.filter(path=>typeof path==='string'&&path.trim()):[];
    if(paths.length){evidence.append(node('p','resource-subtitle',t('historicalTaskPaths')));const list=node('ul','resource-paths');for(const path of paths)list.append(node('li','',path));evidence.append(list);}
    if(session.execution_cwd){evidence.append(node('p','resource-subtitle',t('historicalExecutionCwd')),node('p','resource-path',session.execution_cwd));}
    const links=node('div','artifact-links'),labels={transcript:'originalTranscript',requirements:'originalRequirements',handoff:'originalHandoff'};
    for(const [key,label] of Object.entries(labels)){
      const path=localURL(session.artifact_links?.[key]);if(!path)continue;
      const link=node('a','',t(label));link.href=path;link.setAttribute('download','');links.append(link);
    }
    if(links.children.length)evidence.append(links);
    $('session-evidence').hidden=!$('show-details').checked||!evidence.children.length;
    $('conversation-resources').hidden=!attachments.length&&!missing.length&&$('session-evidence').hidden;
    buildTaskAttachments();updateTaskAttachmentLabels();
  }
  function messageParams(extra={}) {return {library_id:state.libraryId,session_id:state.session?.id,limit:40,details:$('show-details').checked?1:0,...extra};}
  async function openSession(session,around) {
    const epoch=++state.sessionEpoch;
    state.session=session;state.messages=[];state.hasMore=false;state.hasAfter=false;state.followLatest=around===undefined;state.before=null;state.loadingOlder=false;
    $('welcome').hidden=true;$('conversation').hidden=false;$('messages').replaceChildren(node('p','list-empty',t('loading')));$('preview-rail').replaceChildren();$('preview-card').hidden=true;
    $('older-messages').hidden=true;$('conversation-start').hidden=true;$('conversation-end').hidden=true;
    renderConversationHeading();renderSessions();
    const params=messageParams(around===undefined?{}:{around});
    try{
      const [data,preview]=await Promise.all([api(url('/api/messages',params)),api(url('/api/preview',{library_id:state.libraryId,session_id:session.id,limit:60})).catch(()=>({items:[]}))]);
      if(epoch!==state.sessionEpoch)return;
      if(data.session)state.session={...session,...data.session};state.messages=data.items||[];state.hasMore=!!data.has_more;state.hasAfter=!!data.has_after;state.before=data.next_before;
      state.preview=preview.items||[];renderConversationHeading();renderMessages();renderPreview();
      requestAnimationFrame(()=>{if(epoch!==state.sessionEpoch)return;if(around!==undefined)scrollToOrdinal(around);else goLatest();});
    }catch(error){if(epoch===state.sessionEpoch){$('messages').replaceChildren(node('p','list-empty',errorText(error)));toast(errorText(error),true);}}
  }
  function captureAnchor() {
    const scroll=$('message-scroll'),top=scroll.getBoundingClientRect().top;
    const element=[...$('messages').children].find(item=>item.getBoundingClientRect().bottom>top+8);
    return element?{id:element.dataset.messageId,offset:element.getBoundingClientRect().top-top,epoch:state.sessionEpoch}:null;
  }
  function restoreAnchor(anchor) {
    if(!anchor||anchor.epoch!==state.sessionEpoch)return;
    const element=[...$('messages').children].find(item=>item.dataset.messageId===anchor.id);if(!element)return;
    const scroll=$('message-scroll'),offset=element.getBoundingClientRect().top-scroll.getBoundingClientRect().top;
    scroll.scrollTop+=offset-anchor.offset;
  }
  async function loadOlder() {
    if(state.loadingOlder||!state.hasMore||!state.session)return;
    state.loadingOlder=true;const epoch=state.sessionEpoch,anchor=captureAnchor();$('older-messages').disabled=true;
    try{
      const data=await api(url('/api/messages',messageParams({before:state.before})));
      if(epoch!==state.sessionEpoch)return;
      const seen=new Set(state.messages.map(message=>message.id));const earlier=(data.items||[]).filter(message=>!seen.has(message.id));
      state.messages=[...earlier,...state.messages];state.hasMore=!!data.has_more;state.before=data.next_before;
      const fragment=document.createDocumentFragment();for(const message of earlier)fragment.append(renderMessage(message));
      $('messages').prepend(fragment);updateMessageControls();restoreAnchor(anchor);
    }catch(error){if(epoch===state.sessionEpoch)toast(errorText(error),true);}
    finally{if(epoch===state.sessionEpoch){state.loadingOlder=false;$('older-messages').disabled=false;}}
  }
  function renderMessages() {
    const container=$('messages');container.replaceChildren();
    for(const message of state.messages)container.append(renderMessage(message));
    if(!state.messages.length)container.append(node('p','list-empty',t('noVisibleMessages')));
    updateMessageControls();
  }
  function updateMessageControls() {
    $('older-messages').hidden=!state.hasMore;$('conversation-start').hidden=state.hasMore;$('conversation-end').hidden=false;
    $('conversation-end').textContent=t(state.hasAfter?'laterMessages':'endOfConversation');updateLatest();
  }
  function renderMessage(message) {
    const role=['user','assistant','tool','system'].includes(message.role)?message.role:'system';
    const article=node('article','message '+role);article.dataset.messageId=message.id;article.dataset.ordinal=message.ordinal;article.tabIndex=-1;
    const head=node('div','message-head');head.append(node('span','role-symbol',role==='user'?'●':role==='assistant'?'✳':role==='tool'?'⌁':'·'),node('span','message-role',roleName(role)));
    if(message.created_at)head.append(node('time','message-time',displayDate(message.created_at,true)));
    if(message.kind&&!['text','history','image','attachment'].includes(message.kind))head.append(node('span','message-time',t(message.kind)==message.kind?t('technical'):t(message.kind)));
    article.append(head);const body=node('div','message-body');article.append(body);
    const media=Array.isArray(message.media)?message.media:[];
    const missing=Array.isArray(message.metadata?.missing_media)?message.metadata.missing_media:message.metadata?.missing_media?[{}]:[];
    const epoch=state.sessionEpoch;
    const onImageSettled=()=>{if(article.isConnected&&epoch===state.sessionEpoch&&state.followLatest&&!state.hasAfter)goLatest();};
    const sourceMap=new Map();
    for(const item of media){const path=localURL(item.url,'/api/media/');if(!path)continue;for(const key of [item.id,item.name,item.source_path,item.original_path])if(key)sourceMap.set(key,path);}
    const imageReferences=message.metadata?.image_references;
    if(imageReferences&&typeof imageReferences==='object'&&!Array.isArray(imageReferences)){
      for(const [reference,mediaId] of Object.entries(imageReferences)){const path=sourceMap.get(mediaId);if(path)sourceMap.set(reference,path);}
    }
    const inlineImages=new Set();
    const hideAttachmentSource=message.kind==='attachment'&&!$('show-details').checked&&(media.length||missing.length);
    if(!hideAttachmentSource){
      if(window.ArchiveMarkdown){window.ArchiveMarkdown.render(body,message.text||'',{query:$('search').value.trim(),resolveImage:source=>{const path=sourceMap.get(source)||sourceMap.get(String(source).split('/').pop())||null;if(path)inlineImages.add(path);return path;},onImageSettled});}
      else body.textContent=message.text||'';
    }
    if(media.length){
      const gallery=node('div','message-gallery');
      for(const item of media){
        const path=localURL(item.url,'/api/media/');if(!path||inlineImages.has(path))continue;
        if((item.mime||'').startsWith('image/')){
          const name=item.alt||item.name||'';
          const button=node('button','message-image');button.type='button';button.setAttribute('aria-label',t('openImage')+(name?': '+name:''));
          const image=node('img');image.src=path;image.alt=name;image.loading='lazy';image.decoding='async';image.addEventListener('load',onImageSettled,{once:true});
          button.append(image);button.addEventListener('click',()=>showImage(path,name));gallery.append(button);
        }else{const link=node('a','message-attachment',item.name||item.id);link.href=path;link.setAttribute('download','');gallery.append(link);}
      }
      if(gallery.children.length)body.append(gallery);
    }
    if(missing.length)body.append(node('p','message-media-missing',t('missingImage')+(missing.length>1?' ('+missing.length+')':'')));
    body.addEventListener('click',event=>{const image=event.target.closest('.md-image');if(image){const safe=localURL(image.src,'/api/media/');if(safe)showImage(safe,image.alt||'');}});
    if($('show-details').checked&&message.source_path){const source=node('small','message-source',t('source')+': '+message.source_path+(message.source_line?':'+message.source_line:''));article.append(source);}
    // Native scroll anchoring keeps an existing visible message fixed as images
    // above it settle. The explicit prepend restoration handles initial insertion.
    return article;
  }
  function isNearLatest(distance=140){const scroll=$('message-scroll');return !state.hasAfter&&scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<distance;}
  function goLatest(){if(state.hasAfter&&state.session){openSession(state.session);return;}const scroll=$('message-scroll');state.followLatest=true;scroll.scrollTop=scroll.scrollHeight;$('latest').hidden=true;}
  function updateLatest(){$('latest').hidden=!state.session||isNearLatest();}
  function showImage(path,name){$('full-image').src=path;$('full-image').alt=name;$('full-image-name').textContent=name;openDialog('image-dialog');}

  function renderPreview() {
    const rail=$('preview-rail');rail.replaceChildren();
    for(const message of state.preview){const mark=node('button','preview-mark '+message.role);mark.type='button';mark.setAttribute('aria-label',roleName(message.role)+': '+excerpt(message.text,90));
      mark.addEventListener('mouseenter',()=>showPreview(message,mark));mark.addEventListener('focus',()=>showPreview(message,mark));mark.addEventListener('click',()=>showPreview(message,mark));mark.addEventListener('mouseleave',hidePreviewSoon);mark.addEventListener('blur',hidePreviewSoon);rail.append(mark);}
  }
  function showPreview(message,mark) {
    clearTimeout(previewTimer);state.previewMessage=message;
    $('preview-role').textContent=roleName(message.role);$('preview-time').textContent=message.created_at?displayDate(message.created_at):'';$('preview-text').textContent=String(message.text||t('noPreview')).slice(0,900);
    const card=$('preview-card');card.hidden=false;const height=card.getBoundingClientRect().height;
    card.style.top=Math.max(85,Math.min(window.innerHeight-height-15,mark.getBoundingClientRect().top-height/2))+'px';
  }
  function hidePreviewSoon(){clearTimeout(previewTimer);previewTimer=setTimeout(()=>{$('preview-card').hidden=true;},250);}
  async function locatePreview(){
    const message=state.previewMessage;if(!message||!state.session)return;$('preview-card').hidden=true;
    if(!scrollToOrdinal(message.ordinal))await openSession(state.session,message.ordinal);
  }
  function scrollToOrdinal(ordinal){
    const target=[...$('messages').children].find(message=>Number(message.dataset.ordinal)===Number(ordinal));
    if(!target)return false;target.scrollIntoView({block:'center',behavior:'auto'});target.classList.remove('focused');void target.offsetWidth;target.classList.add('focused');target.focus({preventScroll:true});updateLatest();return true;
  }

  function openImport() {
    $('import-error').textContent='';renderImportLibraryOptions();$('import-library').value=state.libraryId||'__new__';
    if(!state.libraryId&&!$('import-name').value.trim())$('import-name').value=t('libraryNamePlaceholder');
    importDestinationChanged();openDialog('import-dialog');
  }
  async function submitImport(event) {
    event.preventDefault();$('import-error').textContent='';const button=$('import-submit');button.disabled=true;
    try{
      let id=$('import-library').value;
      if(id==='__new__'){const result=await api('/api/libraries',{method:'POST',body:{name:$('import-name').value.trim()||t('libraryNamePlaceholder')}});id=result.id;await refreshState(id);}
      const path=cleanPath($('import-path').value);if(!path)throw new Error(t('invalidPath'));
      const body={library_id:id,path,provider:$('import-provider').value};if($('import-dataset').value.trim())body.dataset=$('import-dataset').value.trim();
      const result=await api('/api/import',{method:'POST',body});closeDialog('import-dialog');await watchJob(result.job_id,'import',id);
    }catch(error){$('import-error').textContent=errorText(error);}finally{button.disabled=false;}
  }
  async function createLibrary(event){
    event.preventDefault();$('new-error').textContent='';const button=event.submitter;button.disabled=true;
    try{const result=await api('/api/libraries',{method:'POST',body:{name:$('new-name').value.trim()}});await refreshState(result.id);await chooseLibrary(result.id);closeDialog('new-dialog');$('new-form').reset();toast(t('libraryCreated'));}
    catch(error){$('new-error').textContent=errorText(error);}finally{button.disabled=false;}
  }
  async function submitBackup(event){
    event.preventDefault();$('backup-error').textContent='';const button=event.submitter;button.disabled=true;
    try{const body={library_id:state.libraryId};if(cleanPath($('backup-path').value))body.path=cleanPath($('backup-path').value);const result=await api('/api/backup',{method:'POST',body});closeDialog('backup-dialog');await watchJob(result.job_id,'backup',state.libraryId);}
    catch(error){$('backup-error').textContent=errorText(error);}finally{button.disabled=false;}
  }
  async function submitRestore(event){
    event.preventDefault();$('backup-error').textContent='';const button=event.submitter;button.disabled=true;
    try{const body={path:cleanPath($('restore-path').value)};if($('restore-name').value.trim())body.name=$('restore-name').value.trim();const result=await api('/api/restore',{method:'POST',body});closeDialog('backup-dialog');await watchJob(result.job_id,'restore');}
    catch(error){$('backup-error').textContent=errorText(error);}finally{button.disabled=false;}
  }
  async function createDemo(){
    $('demo-open').disabled=true;
    try{const result=await api('/api/demo',{method:'POST'});if(result.job_id)await watchJob(result.job_id,'demo');else{await refreshState(result.id);await chooseLibrary(result.id);toast(t('demoComplete'));}}
    catch(error){toast(errorText(error),true);}finally{$('demo-open').disabled=false;}
  }
  const jobNames={import:'importing',backup:'backingUp',restore:'restoring',demo:'preparingDemo'};
  const phaseNames={queued:'queued',snapshot:'phaseSnapshot',copying:'phaseSnapshot',extracting:'phaseExtract',parse:'phaseParse',parsing:'phaseParse',index:'phaseIndex',indexing:'phaseIndex',publish:'phasePublish',publishing:'phasePublish',validate:'phaseVerify',verifying:'phaseVerify',backup:'phaseBackup',restore:'phaseRestore',complete:'finished'};
  async function watchJob(id,kind,libraryId){
    const epoch=++state.jobEpoch;state.activeJob=id;state.activeJobKind=kind;state.jobFinished=false;state.jobSnapshot=null;
    $('job-panel').hidden=false;$('job-title').textContent=t(jobNames[kind]||'running');$('job-phase').textContent=t('queued');$('job-result').replaceChildren();$('job-progress').removeAttribute('value');
    while(epoch===state.jobEpoch){
      let job;
      try{job=await api('/api/jobs/'+encodeURIComponent(id));}catch(error){if(epoch===state.jobEpoch){$('job-phase').textContent=errorText(error);$('job-progress').hidden=true;}return;}
      if(epoch!==state.jobEpoch)return;
      state.jobSnapshot=job;
      $('job-progress').hidden=false;
      if(Number.isFinite(job.progress))$('job-progress').value=Math.max(0,Math.min(100,job.progress));else $('job-progress').removeAttribute('value');
      $('job-phase').textContent=t(phaseNames[job.phase]||(job.status==='queued'?'queued':'unknownPhase'));
      if(job.status==='failed'){$('job-title').textContent=t('failed');$('job-phase').textContent=errorText(job.error||t('error'));$('job-progress').hidden=true;state.jobFinished=true;return;}
      if(job.status==='complete'){
        state.jobFinished=true;$('job-title').textContent=t('finished');$('job-progress').value=100;renderJobResult(kind,job.result||{});
        const target=kind==='restore'||kind==='demo'?job.result?.id||job.result?.library_id:libraryId;
        if(kind!=='backup'){
          const oldId=state.libraryId,selected=state.session;await refreshState(target);
          if(state.libraryId!==oldId||!selected)await chooseLibrary(state.libraryId);else{await loadSessions();const current=state.sessions.find(item=>item.id===selected.id);if(current)await openSession(current);}
        }
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,600));
    }
  }
  function renderJobResult(kind,result){
    const container=$('job-result');container.replaceChildren();$('job-phase').textContent=t(kind==='backup'?'backupComplete':kind==='restore'?'restoreComplete':kind==='demo'?'demoComplete':'importComplete');
    if(result.sessions!==undefined||result.messages!==undefined){const metrics=node('div','job-metrics');for(const [value,key] of [[result.sessions,'sessionsImported'],[result.messages,'messageCount']])if(value!==undefined){const item=node('div');item.append(node('strong','',value),node('span','',t(key)));metrics.append(item);}container.append(metrics);}
    if(result.unchanged)container.append(node('p','',t('duplicatesSkipped')));
    if(result.path){const line=node('p','',t('outputPath')+': ');line.append(node('code','',result.path));container.append(line);}
    const download=localURL(result.download_url);
    if(download){const link=node('a','',t('downloadBackup'));link.href=download;link.setAttribute('download','');container.append(link);}
    const warnings=Array.isArray(result.warnings)?result.warnings:[];
    if(warnings.length){const details=node('details'),summary=node('summary','',t('warnings')+' · '+warnings.length),list=node('ul');for(const item of warnings){list.append(node('li','',typeof item==='string'?item:[item.file||item.source_path,item.line?':'+item.line:'',item.error||item.message||item.code].filter(Boolean).join(' ')));}details.append(summary,list);container.append(details);}
  }

  function handoffURL(path='/api/handoff') {
    const context=state.handoffContext;
    return url(path,{library_id:context?.libraryId,session_id:context?.sessionId,target:$('handoff-target').value,lang:I.language});
  }
  async function openHandoff(){
    if(!state.session)return;
    state.handoffContext={libraryId:state.libraryId,sessionId:state.session.id};state.notesDirty=false;$('handoff-error').textContent='';$('handoff-prompt').textContent=t('loading');
    for(const key of noteKeys)$('note-'+key).value='';openDialog('handoff-dialog');await fetchHandoff();
  }
  async function fetchHandoff(){
    const epoch=++state.handoffEpoch;
    try{const data=await api(handoffURL());if(epoch!==state.handoffEpoch)return;state.handoff=data;
      if(!state.notesDirty)for(const key of noteKeys){const value=data.notes?.[key];$('note-'+key).value=Array.isArray(value)?value.join('\n'):typeof value==='object'?JSON.stringify(value,null,2):String(value||'');}
      $('handoff-prompt').textContent=data.prompt||'';}
    catch(error){if(epoch===state.handoffEpoch)$('handoff-error').textContent=errorText(error);}
  }
  async function saveNotes(showStatus=true){
    if(!state.handoffContext)return false;$('handoff-error').textContent='';$('save-notes').disabled=true;
    const notes=Object.fromEntries(noteKeys.map(key=>[key,$('note-'+key).value]));
    try{await api(handoffURL(),{method:'PUT',body:{notes}});state.notesDirty=false;if(showStatus)$('handoff-save-status').textContent=t('saved');await fetchHandoff();return true;}
    catch(error){$('handoff-error').textContent=errorText(error);return false;}finally{$('save-notes').disabled=false;}
  }
  async function copyHandoff(){
    $('copy-handoff').disabled=true;
    try{if(state.notesDirty&&!await saveNotes())return;if(!state.handoff?.prompt){await fetchHandoff();if(!state.handoff?.prompt)return;}
      try{if(!navigator.clipboard?.writeText)throw new Error();await navigator.clipboard.writeText(state.handoff.prompt);toast(t('copied'));}
      catch(_){$('copy-text').value=state.handoff.prompt;openDialog('copy-dialog');$('copy-text').focus();$('copy-text').select();}}
    finally{$('copy-handoff').disabled=false;}
  }
  async function downloadHandoff(){
    $('download-handoff').disabled=true;
    try{if(state.notesDirty&&!await saveNotes())return;const link=node('a');link.href=handoffURL('/api/handoff/download');link.setAttribute('download','');document.body.append(link);link.click();link.remove();}
    finally{$('download-handoff').disabled=false;}
  }
  async function changeHandoffTarget(){if(state.notesDirty){if(!await saveNotes())return;}await fetchHandoff();}

  function applyLanguage(){
    I.apply();$('language-select').value=I.language;$('message-scroll').setAttribute('aria-label',t('conversations'));$('preview-rail').setAttribute('aria-label',t('messagePreviews'));$('job-close').setAttribute('aria-label',t('close'));
    renderLibraryOptions();renderSessions();renderConversationHeading();renderPreview();
    const anchor=captureAnchor(),latest=isNearLatest();if(state.session)renderMessages();requestAnimationFrame(()=>latest?goLatest():restoreAnchor(anchor));
    if($('handoff-dialog').open){if(state.notesDirty)$('handoff-save-status').textContent=t('notesUnsaved');else fetchHandoff();}
    if(state.activeJob){
      const job=state.jobSnapshot;
      $('job-title').textContent=t(job?.status==='complete'?'finished':job?.status==='failed'?'failed':jobNames[state.activeJobKind]||'running');
      if(job?.status==='complete')renderJobResult(state.activeJobKind,job.result||{});
      else if(job?.status==='failed')$('job-phase').textContent=errorText(job.error||t('error'));
      else $('job-phase').textContent=t(phaseNames[job?.phase]||(job?.status==='queued'?'queued':'unknownPhase'));
    }
  }
  function bindEvents(){
    $('library-select').addEventListener('change',()=>chooseLibrary($('library-select').value).catch(error=>toast(errorText(error),true)));
    $('new-library').addEventListener('click',()=>{$('new-error').textContent='';openDialog('new-dialog');});
    $('new-form').addEventListener('submit',createLibrary);
    for(const id of ['import-open','welcome-import'])$(id).addEventListener('click',openImport);
    $('import-library').addEventListener('change',importDestinationChanged);$('import-form').addEventListener('submit',submitImport);
    $('backup-open').addEventListener('click',()=>{$('backup-error').textContent='';$('backup-submit').disabled=!state.libraryId;openDialog('backup-dialog');});
    $('backup-form').addEventListener('submit',submitBackup);$('restore-form').addEventListener('submit',submitRestore);$('demo-open').addEventListener('click',createDemo);
    $('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadSessions(),230);});
    for(const id of ['provider-filter','project-filter','all-records'])$(id).addEventListener('change',()=>loadSessions());
    $('more-sessions').addEventListener('click',()=>loadSessions(true));$('older-messages').addEventListener('click',loadOlder);$('latest').addEventListener('click',goLatest);
    $('message-scroll').addEventListener('scroll',()=>{state.followLatest=isNearLatest(80);updateLatest();if($('message-scroll').scrollTop<90&&state.messages.length&&state.hasMore&&!state.loadingOlder)loadOlder();},{passive:true});
    $('show-details').addEventListener('change',()=>{if(state.session)openSession(state.session);});
    $('session-attachments').addEventListener('toggle',()=>{buildTaskAttachments();updateLatest();});
    $('preview-card').addEventListener('mouseenter',()=>clearTimeout(previewTimer));$('preview-card').addEventListener('mouseleave',hidePreviewSoon);$('preview-card').addEventListener('focusin',()=>clearTimeout(previewTimer));$('preview-card').addEventListener('focusout',hidePreviewSoon);$('preview-locate').addEventListener('click',locatePreview);
    $('handoff-open').addEventListener('click',openHandoff);$('handoff-target').addEventListener('change',changeHandoffTarget);$('save-notes').addEventListener('click',()=>saveNotes());$('copy-handoff').addEventListener('click',copyHandoff);$('download-handoff').addEventListener('click',downloadHandoff);
    for(const key of noteKeys)$('note-'+key).addEventListener('input',()=>{state.notesDirty=true;$('handoff-save-status').textContent=t('notesUnsaved');});
    $('language-select').addEventListener('change',()=>{I.setLanguage($('language-select').value);applyLanguage();});
    $('job-close').addEventListener('click',()=>{$('job-panel').hidden=true;});
    for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>closeDialog(button.dataset.close));
    for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();});
    $('image-dialog').addEventListener('close',()=>{$('full-image').removeAttribute('src');});
    document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();$('search').focus();$('search').select();}if(event.key==='Escape')$('preview-card').hidden=true;});
    window.addEventListener('resize',()=>{$('preview-card').hidden=true;});
  }
  async function init(){
    I.apply();$('language-select').value=I.language;bindEvents();
    try{await refreshState();await loadSessions();}catch(error){toast(t('loadFailed')+' '+errorText(error),true);}
  }
  init();
})();
