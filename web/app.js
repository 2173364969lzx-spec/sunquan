'use strict';
const $ = id => document.getElementById(id);
let activeView = 'today', course = '', state = null, busy = false;
let memoryRequest = 0, sourceRequest = 0, historyRequest = 0;
let memoryRevision = null, correction = null, sourceDetailRequest = 0;
const sessions = new Map();
try { Object.entries(JSON.parse(localStorage.getItem('study-sessions') || '{}')).forEach(([k,v]) => sessions.set(k,v)); } catch (_) {}
const el = (tag, cls, content) => { const n = document.createElement(tag); if (cls) n.className = cls; if (content !== undefined) n.textContent = content; return n; };
function inlineText(target, value) {
  // Recognized markup becomes DOM nodes; HTML, links and formulas stay literal.
  const pattern = /\$\$[\s\S]*?\$\$|\$[^\n$]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|`([^`\n]+)`|\*\*([^*]+)\*\*/g;
  let end = 0, match;
  while ((match = pattern.exec(value))) {
    target.append(document.createTextNode(value.slice(end, match.index)));
    target.append(match[1] !== undefined ? el('code','',match[1]) : match[2] !== undefined ? el('strong','',match[2]) : document.createTextNode(match[0]));
    end = pattern.lastIndex;
  }
  target.append(document.createTextNode(value.slice(end)));
}
function markdownText(value) {
  const container = el('div','message-text formatted'), lines = String(value ?? '').replace(/\r\n?/g,'\n').split('\n');
  let paragraph = [], list = null;
  const flush = () => { if (paragraph.length) { const p = el('p'); inlineText(p,paragraph.join('\n')); container.append(p); paragraph = []; } list = null; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], fence = line.match(/^\s*(`{3,}|~{3,})\s*([^\s]*)\s*$/);
    if (fence) {
      flush(); const codeLines = [], marker = fence[1][0], count = fence[1].length;
      while (++i < lines.length) { const close = lines[i].trim(); if (close.length >= count && [...close].every(c => c === marker)) break; codeLines.push(lines[i]); }
      const pre = el('pre','code-block'), code = el('code','',codeLines.join('\n')); if (fence[2]) code.setAttribute('aria-label',fence[2] + ' 代码'); pre.append(code); container.append(pre); continue;
    }
    if (/^\s*(\$\$|\\\[)/.test(line)) {
      flush(); const math = [line], closer = line.trim().startsWith('$$') ? '$$' : '\\]';
      const opened = line.trim().startsWith('$$') ? line.trim().slice(2) : line.trim().slice(2);
      if (!opened.includes(closer)) while (i + 1 < lines.length) { math.push(lines[++i]); if (lines[i].includes(closer)) break; }
      container.append(el('p','math-literal',math.join('\n'))); continue;
    }
    if (!line.trim()) { flush(); continue; }
    const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
    if (heading) { flush(); const h = el(heading[1].length === 1 ? 'h2' : heading[1].length === 2 ? 'h3' : 'h4'); inlineText(h,heading[2]); container.append(h); continue; }
    const item = line.match(/^\s*(?:([-+*])\s+|(\d+)[.)]\s+)(.+)$/);
    if (item) {
      if (paragraph.length) flush(); const type = item[2] ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== type) { list = el(type); if (item[2]) list.start = Math.max(1,Number(item[2])); container.append(list); }
      const li = el('li'); inlineText(li,item[3]); list.append(li); continue;
    }
    list = null; paragraph.push(line);
  }
  flush(); return container;
}
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); $('notice').hidden = !message; }
async function api(path, body, timeout = 20000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {credentials:'same-origin', signal:controller.signal, ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Study-Request':'1'}, body:JSON.stringify(body)})});
    let result; try { result = await response.json(); } catch (_) { throw new Error('服务返回了无法读取的内容，请稍后重试。'); }
    if (!response.ok) throw new Error(result.error || '操作未完成，请稍后重试。');
    return result;
  } catch (error) { if (error.name === 'AbortError') throw new Error(timeout > 30000 ? '等待回答超时。本地模型可能仍在处理，请稍后重试。' : '连接超时，请确认学习伙伴仍在运行。'); if (error instanceof TypeError) throw new Error('暂时连接不到本地服务，请确认学习伙伴仍在运行。'); throw error;
  } finally { clearTimeout(timer); }
}
function query(path, params) { return path + '?' + new URLSearchParams(params); }
function key() { return globalThis.crypto?.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2); }
function saveSessions() { try { localStorage.setItem('study-sessions', JSON.stringify(Object.fromEntries(sessions))); } catch (_) {} }
function localDate(value) { if (!value) return ''; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
function showView(view, focus = false) {
  if (!['today','learn','memory','sources'].includes(view)) view = 'today';
  activeView = view;
  document.querySelectorAll('.view').forEach(n => n.hidden = n.id !== 'view-' + view);
  document.querySelectorAll('.nav button').forEach(n => { if (n.dataset.view === view) n.setAttribute('aria-current','page'); else n.removeAttribute('aria-current'); });
  if (view === 'memory') loadMemory();
  if (view === 'sources') loadSources();
  if (view === 'learn') loadHistory();
  if (focus) $('main').focus({preventScroll:true});
}
function renderReminder(reminder = {}) {
  $('reminder-body').textContent = '点击“现在开始”，把这一步拆成一个小问题。';
  let message = '';
  if (['snoozed','snooze'].includes(reminder.status)) message = reminder.snoozed_until ? '稍后再继续 · ' + localDate(reminder.snoozed_until) : '已安排稍后提醒。';
  else if (['skipped','skip','dismissed'].includes(reminder.status)) message = '今天好好休息，想继续时随时回来。';
  else if (['started','start'].includes(reminder.status)) message = '已准备好。写下你的问题，我们从一小步开始。';
  $('reminder-status').textContent = message;
}
async function loadState() {
  try {
    state = await api('/api/state');
    $('date').textContent = state.date || new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
    $('next-step').textContent = state.snapshot?.next_step || '先花5分钟，查看一条课程通知。';
    $('summary').textContent = state.snapshot?.summary || '还没有学习记录。先问一个课程问题，或在记忆里记下你目前学到哪儿。';
    $('document-count').textContent = state.stats?.documents ?? 0;
    $('model-status').textContent = state.model?.available ? '本地模型已就绪' : '本地模型暂未就绪';
    $('mobile-model-status').textContent = $('model-status').textContent;
    $('mobile-model-status').classList.toggle('unavailable', !state.model?.available);
    $('model-name').textContent = [state.model?.name, state.model?.status].filter(Boolean).join(' · ') || '本地学习伙伴';
    $('model-dot').classList.toggle('online', !!state.model?.available);
    $('source-root').textContent = state.source_root ? '本地资料位置：' + state.source_root : '';
    $('course').replaceChildren(el('option','', '全部课程')); $('course').firstChild.value = '';
    (state.courses || []).forEach(c => { const option = el('option','',c.title); option.value = c.id; $('course').append(option); });
    if ([...$('course').options].some(o => o.value === course)) $('course').value = course; else course = '';
    renderReminder(state.reminder);
  } catch (error) { $('model-status').textContent = '本地服务连接失败'; $('mobile-model-status').textContent = '本地服务连接失败'; $('mobile-model-status').classList.add('unavailable'); notice(error.message,true); }
}
function empty(target, title, detail) { target.replaceChildren(el('div','empty-state')); target.firstChild.append(el('h2','',title),el('p','',detail)); }
function cancelCorrection(clear = true) {
  correction = null; $('memory-kind').disabled = false; $('cancel-correction').hidden = true;
  $('memory-label').textContent = '记下一条学习线索'; $('memory-target').textContent = '保存到当前课程 · 由你记录'; $('save-memory').textContent = '保存记忆';
  if (clear) $('memory-content').value = '';
}
function editMemory(item) {
  correction = {id:item.id,course:item.course,revision:memoryRevision};
  $('memory-content').value = item.content; $('memory-kind').disabled = true; $('cancel-correction').hidden = false;
  $('memory-label').textContent = '更正这条学习记忆'; $('memory-target').textContent = '更正保存到：' + item.course + ' · 原记录会被替换'; $('save-memory').textContent = '保存更正';
  $('memory-form').scrollIntoView({block:'nearest'}); $('memory-content').focus();
}
async function loadMemory() {
  const request = ++memoryRequest, target = $('memory-list'); target.replaceChildren(el('p','loading','正在读取学习记忆…'));
  try {
    const data = await api(query('/api/memory',{course})); if (request !== memoryRequest) return;
    memoryRevision = data.revision;
    $('memory-count').textContent = (data.items || []).length + ' 条';
    $('handoff-text').textContent = typeof data.handoff === 'string' ? data.handoff : data.handoff ? JSON.stringify(data.handoff,null,2) : '尚无交接内容。';
    target.replaceChildren();
    if (!data.items?.length) { empty(target,'还没有新增学习记忆','原有课程档案已在“资料”中；这里显示你主动新增的记忆。记下真实进度，下次就能接着学。'); return; }
    data.items.forEach(item => {
      const card = el('article','memory-item'), meta = el('div','memory-meta'), controls = el('div','memory-actions'), edit = el('button','text-button','更正'), remove = el('button','delete-memory','删除'); remove.type = 'button'; edit.type = 'button';
      edit.setAttribute('aria-label','更正记忆：' + String(item.content).slice(0,30)); edit.addEventListener('click',()=>editMemory(item));
      remove.setAttribute('aria-label','删除记忆：' + String(item.content).slice(0,30));
      remove.addEventListener('click',async () => { if (!confirm('删除这条学习记忆？')) return; remove.disabled = true; try { await api('/api/memory/delete',{id:item.id}); if(correction?.id===item.id)cancelCorrection(); notice('记忆已删除。'); loadMemory(); loadState(); } catch(error) { notice(error.message,true); remove.disabled = false; } });
      controls.append(edit,remove); const kindLabel = {user_reported:'实际进度',next_step:'下一步',preference:'学习偏好',correction:'更正',verified:'已核验'}[item.kind] || item.kind;
      meta.append(el('span','hint',item.course + ' · ' + kindLabel + ' · ' + localDate(item.created_at)),controls);
      card.append(el('p','',item.content),meta); target.append(card);
    });
  } catch(error) { if (request === memoryRequest) target.replaceChildren(el('p','error-text',error.message)); }
}
async function openSource(id) {
  const request=++sourceDetailRequest, dialog = $('source-dialog'); $('source-title').textContent = '资料原文'; $('source-path').textContent = ''; $('source-content').textContent = '正在读取原文…'; $('source-preview').replaceChildren(); $('source-preview').hidden=true; $('source-text').open=true; $('source-text-label').textContent='资料文本';
  if (!dialog.open) dialog.showModal();
  try { const data = await api(query('/api/source',{id})); if(request!==sourceDetailRequest)return; $('source-title').textContent = data.title || '资料原文'; $('source-path').textContent = [data.path, data.page ? '第 ' + data.page + ' 页' : ''].filter(Boolean).join(' · '); $('source-content').textContent = data.content || '这份资料暂时没有可读取的文本。';
    if(data.preview){ const image=el('img','page-preview'); image.alt=(data.title || '课程资料') + (data.page ? '，第 ' + data.page + ' 页原图' : '原页图片'); image.src=query('/api/preview',{id}); image.addEventListener('error',()=>{if(request!==sourceDetailRequest)return;image.remove();$('source-preview').append(el('p','error-text','原页图片暂时无法读取，请稍后重试。'));$('source-text').open=true;}); $('source-preview').append(el('p','hint','检索文本可能经过 OCR，公式或符号可能有误，请看原图核对。'),image); $('source-preview').hidden=false; $('source-text').open=false; $('source-text-label').textContent='展开检索文本'; }
  }
  catch(error) { if(request===sourceDetailRequest)$('source-content').textContent = error.message; }
}
async function loadSources() {
  const request = ++sourceRequest, target = $('source-list'); target.replaceChildren(el('p','loading','正在查找资料…'));
  try {
    const data = await api(query('/api/sources',{q:$('search').value.trim(),course})); if (request !== sourceRequest) return;
    target.replaceChildren();
    if (!data.items?.length) { empty(target,'这里还没有匹配的资料','试试其他关键词；添加课程文件后，点击“刷新资料”。'); return; }
    data.items.forEach(item => { const card = el('button','source-card'); card.type='button'; card.append(el('h2','',item.title || '课程资料'),el('p','',item.excerpt || '打开查看资料内容'),el('p','source-meta',[item.kind,item.page ? '第 ' + item.page + ' 页' : '',item.path].filter(Boolean).join(' · '))); card.addEventListener('click',() => openSource(item.id)); target.append(card); });
  } catch(error) { if (request === sourceRequest) target.replaceChildren(el('p','error-text',error.message)); }
}
function sourceChips(sources) {
  const list = el('div','message-sources');
  (sources || []).forEach((source,index) => {
    const title = '[资料' + (index+1) + '] ' + (source.title || '资料出处') + (source.page ? ' · 第 ' + source.page + ' 页' : '');
    let safeURL = null; if (source.url) { try { const url = new URL(source.url); if (['https:','http:'].includes(url.protocol)) safeURL = url.href; } catch(_) {} }
    if (safeURL) { const link = el('a','source-chip',title); link.href=safeURL; link.target='_blank'; link.rel='noopener noreferrer'; list.append(link); }
    else if (source.id) { const button = el('button','source-chip',title); button.type='button'; button.addEventListener('click',()=>openSource(source.id)); list.append(button); }
  }); return list;
}
function message(role, content, sources = [], error = false) {
  $('chat-empty')?.remove(); const node = el('article','message ' + (role === 'user' ? 'user' : 'assistant') + (error ? ' error' : ''));
  node.append(el('div','message-label',role === 'user' ? '你' : error ? '暂时无法回答' : '学习伙伴'),role === 'user' || error ? el('div','message-text',content) : markdownText(content));
  if (sources.length) node.append(sourceChips(sources)); $('chat').append(node); $('chat').scrollTop = $('chat').scrollHeight; return node;
}
function resetChat() { empty($('chat'),'从一个具体问题开始','例如：“这份课程通知里，我这周要做什么？” 回答是学习参考，实际进度由你在记忆中记录。'); $('chat').firstChild.id = 'chat-empty'; }
async function loadHistory() {
  if (busy) return;
  const request = ++historyRequest, selected = course, session = sessions.get(selected);
  if (!session) { resetChat(); return; }
  $('chat').replaceChildren(el('p','loading','正在读取这门课程的对话…'));
  try { const data = await api(query('/api/history',{session_id:session})); if (request !== historyRequest || selected !== course || busy) return; resetChat(); (data.items || []).forEach(item => message(item.role,item.content,item.sources || [])); }
  catch(error) { if (request === historyRequest && selected === course && !busy) { resetChat(); notice(error.message,true); } }
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view,true)));
document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();showView('today',true);});
$('course').addEventListener('change',()=>{course=$('course').value;if(correction)cancelCorrection();notice('');if(activeView==='learn')loadHistory();else if(activeView==='memory')loadMemory();else if(activeView==='sources')loadSources();});
['start','snooze','skip'].forEach(action=>$ (action).addEventListener('click',async()=>{
  const button=$(action);button.disabled=true;
  try { const result=await api('/api/reminder',{action}); renderReminder(result.reminder || result);notice(action==='start'?'准备好了，按发送后才会开始提问。':action==='snooze'?'已延后15分钟。':'已记下今天休息。');if(action==='start'){showView('learn');$('question').value='带我完成今天这个5分钟的小任务。';$('question').focus();} }
  catch(error){notice(error.message,true);}finally{button.disabled=false;}
}));
$('web').addEventListener('change',()=>{$('url-row').hidden=!$('web').checked;});
$('question').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();$('chat-form').requestSubmit();}});
$('chat-form').addEventListener('submit',async event=>{
  event.preventDefault();const question=$('question').value.trim();if(!question||busy)return;
  const selected=course, web=$('web').checked, url=$('url').value.trim();
  if(web&&url){try{const parsed=new URL(url);if(parsed.protocol!=='https:')throw new Error();}catch(_){notice('请输入公开的 https 网页地址。',true);return;}}
  busy=true;++historyRequest;$('send').disabled=true;$('new-chat').disabled=true;$('course').disabled=true;$('question').value='';notice('');message('user',question);
  $('chat-status').textContent='本地模型正在处理，首次加载可能需要几分钟…';
  const waiting=message('assistant','正在读取资料并等待本地模型回答…');waiting.setAttribute('aria-busy','true');
  try {
    const result=await api('/api/chat',{message:question,course:selected,session_id:sessions.get(selected),web,...(web&&url?{url}:{})},200000);waiting.remove();
    if(result.session_id){sessions.set(selected,result.session_id);saveSessions();}
    if(result.error || !result.answer || ['error','unavailable','offline','failed'].includes(result.status)){message('assistant',result.error || '本地模型暂时无法回答，请启动 Ollama 并确认模型可用后重试。',[],true);$('question').value=question;$('chat-status').textContent='这次提问未完成；你的问题已放回输入框。';}
    else {message('assistant',result.answer,result.sources || []);$('chat-status').textContent='回答已生成。实际学习进度可在“记忆”中记录。';}
  }catch(error){waiting.remove();message('assistant',error.message,[],true);$('question').value=question;$('chat-status').textContent='这次提问未完成；你的问题已放回输入框。';}
  finally{busy=false;$('send').disabled=false;$('new-chat').disabled=false;$('course').disabled=false;loadState();}
});
$('new-chat').addEventListener('click',()=>{if(busy)return;sessions.delete(course);saveSessions();++historyRequest;resetChat();$('question').value='';$('chat-status').textContent='新对话已准备好。';$('question').focus();});
$('memory-form').addEventListener('submit',async event=>{
  event.preventDefault();const content=$('memory-content').value.trim();if(!content)return;$('save-memory').disabled=true;
  const editing=correction, selected=course;
  try{await api('/api/memory',{content,course:editing ? editing.course : selected,kind:editing ? 'correction' : $('memory-kind').value,idempotency_key:key(),...(editing?{supersedes:editing.id,expected_revision:editing.revision}:{})});cancelCorrection();notice(editing?'更正已保存，旧记录已停止使用。':'已留下这条学习记忆。');loadMemory();loadState();}catch(error){notice(error.message + (editing?' 更正尚未保存；请重新读取记忆并确认版本。':''),true);}finally{$('save-memory').disabled=false;}
});
$('cancel-correction').addEventListener('click',()=>cancelCorrection());
$('copy-handoff').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('handoff-text').textContent);notice('学习交接已复制。');}catch(_){notice('无法自动复制，请选中交接文字后复制。',true);}});
$('search-form').addEventListener('submit',event=>{event.preventDefault();loadSources();});
$('reindex').addEventListener('click',async()=>{
  $('reindex').disabled=true;$('reindex').textContent='正在刷新…';
  try{const result=await api('/api/reindex',{},200000);const errors=Array.isArray(result.errors)?result.errors.length:Number(result.errors || 0);notice('资料已刷新：'+(result.documents ?? 0)+' 份文件，'+(result.pages ?? 0)+' 页内容。'+(errors?' 有 '+errors+' 项无法读取。':''),errors>0);loadState();loadSources();}catch(error){notice(error.message,true);}finally{$('reindex').disabled=false;$('reindex').textContent='刷新资料';}
});
$('close-source').addEventListener('click',()=>$('source-dialog').close());
$('source-dialog').addEventListener('click',event=>{if(event.target===$('source-dialog')){const rect=$('source-dialog').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('source-dialog').close();}});
loadState();
