'use strict';
const $ = id => document.getElementById(id);
let activeView = 'today', course = '', state = null, busy = false;
let memoryRequest = 0, sourceRequest = 0, historyRequest = 0;
let memoryRevision = null, correction = null, sourceDetailRequest = 0;
let memoryOffset = 0, memoryTotal = 0, memoryDetailRequest = 0;
let pendingChat = null, chatJob = null, waitingMessage = null, pollTimer = null, elapsedTimer = null, jobChecking = false, jobOffline = false, elapsedAt = 0, jobEpoch = 0, restoringChat = false;
try { pendingChat = JSON.parse(localStorage.getItem("study-pending-chat") || "null"); } catch (_) {}
let plan = null, studyRun = null, planRequest = 0, studyBusy = false, detached = false, minutes = 5, easy = false;
try { course = localStorage.getItem('study-course') || ''; } catch (_) {}
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
    if (!response.ok) { const error = new Error(result.error || '操作未完成，请稍后重试。'); error.status = response.status; throw error; }
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
    if (!plan) $('next-step').textContent = state.snapshot?.next_step || '先花5分钟，查看一条课程通知。';
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
  correction = {id:item.id,course:item.course,revision:memoryRevision,kind:item.kind};
  $('memory-content').value = item.content; $('memory-kind').disabled = true; $('cancel-correction').hidden = false;
  $('memory-label').textContent = '更正这条学习记忆'; $('memory-target').textContent = '更正保存到：' + item.course + ' · 旧版本会保留在历史中'; $('save-memory').textContent = '保存更正';
  $('memory-form').scrollIntoView({block:'nearest'}); $('memory-content').focus();
}
const memoryStatus = item => ({active:'生效',replaced:'已更正',revoked:'已撤销'}[item.status || 'active'] || item.status);
const memoryOrigin = value => ({user:'你主动记录',Codex:'Codex 按次交回',codex:'Codex 按次交回',study_stop:'结束小步时的停点',study_check:'小测记录',study_answer:'你提交的小测回答',study_finish:'本次学习记录'}[value] || value || '来源未标明');
function memoryEvidence(item) {
  const data = item.evidence;
  return typeof data === 'string' ? data : data ? JSON.stringify(data,null,2) : '未附加来源依据';
}
async function openMemory(id) {
  const request=++memoryDetailRequest, target=$('memory-detail'); target.replaceChildren(el('p','loading','正在读取来源与版本…'));
  if(!$('memory-dialog').open)$('memory-dialog').showModal();
  try {
    const data=await api(query('/api/memory/detail',{id}));if(request!==memoryDetailRequest)return;
    target.replaceChildren(el('p','hint','版本按记录时间从旧到新排列。更正和撤销会停止旧版本的后续使用，历史与原始资料仍保留。'));
    (data.versions || [data.item]).filter(Boolean).forEach((item,index)=>{
      const card=el('article','memory-version');card.append(el('h3','','版本 '+(index+1)+' · '+memoryStatus(item)+(item.supersedes?' · 更正记录':'')),el('p','memory-content',item.content),el('p','hint',[item.course,memoryOrigin(item.origin),localDate(item.created_at)].filter(Boolean).join(' · ')),el('pre','memory-evidence',memoryEvidence(item)));target.append(card);
    });
  }catch(error){if(request===memoryDetailRequest)target.replaceChildren(el('p','error-text',error.message));}
}
async function loadCoverage() {
  const target=$('memory-coverage');
  try {
    const data=await api('/api/memory/coverage');target.replaceChildren(el('h2','','记忆接入范围'),el('p','',`${data.documents ?? 0} 份资料 · ${data.pages ?? 0} 页 · ${data.text_documents ?? 0} 份文本`),el('p','hint',typeof data.scope==='string'?data.scope: data.scope?JSON.stringify(data.scope):'范围说明暂未提供。'),el('p','hint','未导出的 ChatGPT / Codex 平台聊天不在本地索引中。接入资料数不代表全部聊天历史。'),el('p','hint',data.checked_at?'最近核对：'+localDate(data.checked_at):'核对时间暂未提供。'));
    const gaps=data.not_indexed || [], errors=data.errors || [];
    target.append(el('p','hint',`未接入：${data.not_indexed_total ?? gaps.length} 项 · 读取错误：${errors.length} 项`));
    if(gaps.length||errors.length){const details=el('details');details.append(el('summary','','查看缺口与错误'));[...gaps.map(i=>[i.path,i.reason].filter(Boolean).join(' · ')),...errors.map(i=>[i.path,i.error].filter(Boolean).join(' · '))].forEach(value=>details.append(el('p','hint',value)));target.append(details);}
  }catch(error){target.replaceChildren(el('h2','','记忆接入范围'),el('p','error-text','暂时无法核对覆盖范围：'+error.message),el('p','hint','未导出的平台聊天不在本地索引中；当前接入数量尚未确认。'));}
}
async function loadMemory() {
  const request = ++memoryRequest, target = $('memory-list'); target.replaceChildren(el('p','loading','正在读取学习记忆…'));
  loadCoverage();
  try {
    const data = await api(query('/api/memory',{course,q:$('memory-search').value.trim(),state:$('memory-state').value,offset:memoryOffset,limit:30})); if (request !== memoryRequest) return;
    memoryRevision = data.revision; memoryTotal=data.total ?? (data.items || []).length;
    $('memory-count').textContent = memoryTotal + ' 条';$('memory-page').textContent=memoryTotal?`${memoryOffset+1}–${Math.min(memoryOffset+30,memoryTotal)} / ${memoryTotal}`:'0 条';$('memory-prev').disabled=memoryOffset===0;$('memory-next').disabled=memoryOffset+30>=memoryTotal;
    $('handoff-text').textContent = typeof data.handoff === 'string' ? data.handoff : data.handoff ? JSON.stringify(data.handoff,null,2) : '尚无交接内容。';
    target.replaceChildren();
    if (!data.items?.length) { empty(target,'这里没有匹配的记忆','可以调整关键词或查看历史记忆。原有课程档案仍在“资料”中。'); return; }
    data.items.forEach(item => {
      const card = el('article','memory-item'), meta = el('div','memory-meta'), controls = el('div','memory-actions'), detail=el('button','text-button','来源与历史');detail.type='button';detail.setAttribute('aria-label','查看记忆来源与历史：'+String(item.content).slice(0,30));detail.addEventListener('click',()=>openMemory(item.id));controls.append(detail);
      if((item.status || 'active')==='active'){
        const edit=el('button','text-button','更正'),remove=el('button','delete-memory','撤销');edit.type=remove.type='button';edit.setAttribute('aria-label','更正记忆：'+String(item.content).slice(0,30));edit.addEventListener('click',()=>editMemory(item));remove.setAttribute('aria-label','撤销记忆：'+String(item.content).slice(0,30));
        const displayedRevision=data.revision;
        remove.addEventListener('click',()=>{
          remove.disabled=true;
          const confirmPanel=el('div','revoke-confirmation'),actions=el('div','memory-actions'),confirmButton=el('button','secondary','确认撤销'),keep=el('button','quiet','保留'),reload=el('button','text-button','重新读取记忆'),errorText=el('p','error-text');
          confirmPanel.setAttribute('role','group');confirmPanel.setAttribute('aria-label','确认撤销这条记忆');confirmButton.type=keep.type=reload.type='button';reload.hidden=true;errorText.hidden=true;errorText.setAttribute('role','status');
          confirmPanel.append(el('p','hint','撤销后，这条记忆将停止用于后续学习接续。历史和原始资料仍保留，不会恢复被更正的旧版。'),actions,errorText);actions.append(confirmButton,keep,reload);card.append(confirmPanel);confirmButton.focus();
          keep.addEventListener('click',()=>{confirmPanel.remove();remove.disabled=false;remove.focus();});
          reload.addEventListener('click',()=>loadMemory());
          confirmButton.addEventListener('click',async()=>{
            confirmButton.disabled=keep.disabled=reload.disabled=edit.disabled=true;errorText.hidden=true;
            try{await api('/api/memory/delete',{id:item.id,expected_revision:displayedRevision});if(correction?.id===item.id)cancelCorrection();notice('已撤销，历史与原始资料仍保留。');await loadMemory();loadState();loadPlan();}
            catch(error){errorText.textContent=error.message+' 原记忆仍保留；请重新读取后核对当前版本。';errorText.hidden=false;reload.hidden=false;confirmButton.disabled=keep.disabled=reload.disabled=edit.disabled=false;}
          });
        });controls.append(edit,remove);
      }
      const kindLabel={user_reported:'实际进度',next_step:'下一步',preference:'学习偏好',correction:'更正',verified:'已核验'}[item.kind] || item.kind;
      meta.append(el('span','hint',[item.course,kindLabel,memoryStatus(item),item.supersedes?'更正记录':'',memoryOrigin(item.origin),localDate(item.created_at)].filter(Boolean).join(' · ')),controls);
      const evidence=el('details','memory-source');evidence.append(el('summary','','来源依据'),el('pre','memory-evidence',memoryEvidence(item)));card.append(el('p','',item.content),meta,evidence);target.append(card);
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
  const request = ++historyRequest, selected = course, session = studyRun?.chat_session_id || sessions.get(selected);
  if (!session) { resetChat(); return; }
  $('chat').replaceChildren(el('p','loading','正在读取这门课程的对话…'));
  try { const data = await api(query('/api/history',{session_id:session})); if (request !== historyRequest || selected !== course || busy) return; resetChat(); (data.items || []).forEach(item => message(item.role,item.content,item.sources || [])); }
  catch(error) { if (request === historyRequest && selected === course && !busy) { resetChat(); notice(error.message,true); } }
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view,true)));
document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();showView('today',true);});
$('course').addEventListener('change',()=>{
  if(busy||studyBusy){$('course').value=course;return;}memoryOffset=0;
  course=$('course').value; try { localStorage.setItem('study-course',course); } catch (_) {}
  studyRun=null; plan=null; detached=false; ++historyRequest; ++memoryRequest; ++sourceRequest;
  renderRun(); if(correction)cancelCorrection(); notice(''); loadPlan();
  if(activeView==='learn')loadHistory();else if(activeView==='memory')loadMemory();else if(activeView==='sources')loadSources();
});
['snooze','skip'].forEach(action=>$(action).addEventListener('click',async()=>{
  const button=$(action);button.disabled=true;
  try { const result=await api('/api/reminder',{action}); renderReminder(result.reminder || result);notice(action==='snooze'?'已延后15分钟。':'已记下今天休息。'); }
  catch(error){notice(error.message,true);}finally{button.disabled=false;}
}));
$('web').addEventListener('change',()=>{$('url-row').hidden=!$('web').checked;});
$('question').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();$('chat-form').requestSubmit();}});
$('chat-form').addEventListener('submit',event=>{event.preventDefault();sendQuestion($('question').value.trim());});
function persistPending(value) { localStorage.setItem('study-pending-chat',JSON.stringify(value)); }
function jobStatusText() {
  if(!pendingChat)return;
  const labels={retrieving:'正在检索资料',loading:'正在加载本地模型',generating:'正在生成回答',verifying:'正在核对回答与依据'};
  const elapsed=Math.max(0,Math.floor((chatJob?.elapsed_seconds || 0)+(elapsedAt?(Date.now()-elapsedAt)/1000:0)));
  const text=jobOffline?'连接暂时中断，任务仍保留。可重新检查或取消。':chatJob?.status==='cancelling'?'正在等待本地任务停止…':labels[chatJob?.stage] || '正在确认服务是否接收请求';
  $('chat-status').textContent=text+' · 已等待 '+elapsed+' 秒';
  if(waitingMessage)waitingMessage.querySelector('.message-text').textContent=text+'。回答核对完成后会显示。';
}
function beginWaiting(restored=false) {
  busy=true;updateLocks();++historyRequest;
  $('chat-job-actions').hidden=false;$('check-answer').hidden=true;$('retry-answer').hidden=true;$('cancel-answer').disabled=false;
  if(restored)message('user',pendingChat.payload.message);
  waitingMessage=message('assistant','正在确认请求…');waitingMessage.setAttribute('aria-busy','true');
  clearInterval(elapsedTimer);elapsedTimer=setInterval(jobStatusText,1000);jobStatusText();
}
function pauseJob(error,canRetry=false) {
  clearTimeout(pollTimer);jobOffline=true;jobStatusText();$('check-answer').hidden=false;$('retry-answer').hidden=!canRetry;
  notice(error.message+' 原请求已保留，重新检查不会重复生成回答。',true);
}
function finishJob(job) {
  const reloadCompletedHistory=restoringChat && job.status==='completed' && !!job.result?.answer && !job.result.error;
  const saved=pendingChat, selected=saved.payload.course, run=studyRun?.id===saved.payload.study_run_id?studyRun:null;
  clearTimeout(pollTimer);clearInterval(elapsedTimer);waitingMessage?.remove();waitingMessage=null;
  if(job.status==='completed' && job.result?.answer && !job.result.error){
    const result=job.result;if(result.session_id){sessions.set(selected,result.session_id);saveSessions();if(run)run.chat_session_id=result.session_id;}
    if(!reloadCompletedHistory)message('assistant',result.answer,result.sources || []);
    const grounding=result.grounding?.status;$('chat-status').textContent=grounding==='cited'?'回答附有原文依据，可打开资料核对；引用不代表结论一定正确。':grounding==='insufficient'?'当前资料依据不足，请核对回答中的说明。':grounding==='invalid'?'回答依据未通过检查，请核对原文。':'回答供学习参考，请核对资料。';
  }else{
    $('question').value=saved.payload.message;
    const text=job.status==='cancelled'?'回答已取消，原问题已放回输入框。':job.status==='interrupted'?'本地服务重启使回答中断，原问题已恢复，可重新发送。':job.error || job.result?.error || '这次回答未完成，原问题已恢复。';
    message('assistant',text,[],job.status!=='cancelled');$('chat-status').textContent=text;
  }
  pendingChat=null;chatJob=null;busy=false;jobOffline=false;$('chat-job-actions').hidden=true;try{localStorage.removeItem('study-pending-chat');}catch(_){}updateLocks();loadState();restoringChat=false;if(reloadCompletedHistory)loadHistory();
}
function applyJob(job) {
  if(!pendingChat || job.id!==pendingChat.key)return;
  chatJob=job;elapsedAt=Date.now();jobOffline=false;notice('');$('check-answer').hidden=true;$('retry-answer').hidden=true;$('cancel-answer').disabled=job.status==='cancelling';jobStatusText();
  if(['completed','cancelled','failed','interrupted'].includes(job.status)){finishJob(job);return;}
  clearTimeout(pollTimer);pollTimer=setTimeout(checkJob,1500);
}
async function checkJob() {
  if(!pendingChat)return;if(jobChecking){clearTimeout(pollTimer);pollTimer=setTimeout(checkJob,1500);return;}jobChecking=true;const id=pendingChat.key, epoch=jobEpoch;
  try{const job=await api(query('/api/chat/job',{id}));if(pendingChat?.key===id && epoch===jobEpoch)applyJob(job);}catch(error){if(pendingChat?.key===id && epoch===jobEpoch)pauseJob(error,error.status===404);}finally{jobChecking=false;}
}
async function startPendingJob() {
  if(!pendingChat||jobChecking)return;jobChecking=true;const saved=pendingChat, epoch=jobEpoch;$('retry-answer').disabled=true;
  try{const job=await api('/api/chat/start',{...saved.payload,idempotency_key:saved.key});if(pendingChat?.key===saved.key && epoch===jobEpoch)applyJob(job);}catch(error){if(pendingChat?.key===saved.key && epoch===jobEpoch){if(error.status>=400 && error.status<500)finishJob({status:'failed',error:error.message});else pauseJob(error,true);}}finally{jobChecking=false;$('retry-answer').disabled=false;}
}
async function sendQuestion(question, automatic=false) {
  if(!question||busy||studyBusy)return;
  const selected=studyRun?.course || course, run=studyRun, web=automatic?false:$('web').checked, url=$('url').value.trim();
  if(web&&url){try{const parsed=new URL(url);if(parsed.protocol!=='https:')throw new Error();}catch(_){notice('请输入公开的 https 网页地址。',true);return;}}
  const saved={key:key(),payload:{message:question,course:selected,session_id:run?.chat_session_id || sessions.get(selected),web,...(run?{study_run_id:run.id}:{}),...(web&&url?{url}:{})}};
  try{persistPending(saved);}catch(_){notice('浏览器无法保存待处理请求。请允许本地存储后再发送，以便中断后恢复。',true);return;}
  pendingChat=saved;chatJob=null;elapsedAt=Date.now();jobOffline=false;$('question').value='';notice('');message('user',question);beginWaiting();await startPendingJob();
}
$('check-answer').addEventListener('click',checkJob);
$('retry-answer').addEventListener('click',startPendingJob);
$('cancel-answer').addEventListener('click',async()=>{
  if(!pendingChat)return;const id=pendingChat.key;++jobEpoch;$('cancel-answer').disabled=true;clearTimeout(pollTimer);
  try{const job=await api('/api/chat/cancel',{id});if(pendingChat?.key===id)applyJob(job);}catch(error){if(pendingChat?.key===id){$('cancel-answer').disabled=false;pauseJob(error,error.status===404);}}
});
$('new-chat').addEventListener('click',()=>{
  if(busy||studyBusy)return; detached=true;studyRun=null;renderRun();sessions.delete(course);saveSessions();++historyRequest;resetChat();$('question').value='';$('chat-status').textContent='新对话已准备好。今日的小步仍可继续。';$('question').focus();
});
$('memory-form').addEventListener('submit',async event=>{
  event.preventDefault();const content=$('memory-content').value.trim();if(!content)return;$('save-memory').disabled=true;
  const editing=correction, selected=course;
  try{await api('/api/memory',{content,course:editing ? editing.course : selected,kind:editing ? editing.kind : $('memory-kind').value,idempotency_key:key(),...(editing?{supersedes:editing.id,expected_revision:editing.revision}:{})});cancelCorrection();notice(editing?'更正已保存，旧记录已停止使用。':'已留下这条学习记忆。');loadMemory();loadState();loadPlan();}catch(error){notice(error.message + (editing?' 更正尚未保存；请重新读取记忆并确认版本。':''),true);}finally{$('save-memory').disabled=false;}
});
$('memory-search-form').addEventListener('submit',event=>{event.preventDefault();memoryOffset=0;loadMemory();});
$('memory-state').addEventListener('change',()=>{memoryOffset=0;loadMemory();});
$('memory-prev').addEventListener('click',()=>{memoryOffset=Math.max(0,memoryOffset-30);loadMemory();});
$('memory-next').addEventListener('click',()=>{memoryOffset+=30;loadMemory();});
$('close-memory').addEventListener('click',()=>{$('memory-dialog').close();++memoryDetailRequest;});
$('cancel-correction').addEventListener('click',()=>cancelCorrection());
$('copy-handoff').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('handoff-text').textContent);notice('学习交接已复制。');}catch(_){notice('无法自动复制，请选中交接文字后复制。',true);}});
$('search-form').addEventListener('submit',event=>{event.preventDefault();loadSources();});
$('reindex').addEventListener('click',async()=>{
  $('reindex').disabled=true;$('reindex').textContent='正在刷新…';
  try{const result=await api('/api/reindex',{},200000);const errors=Array.isArray(result.errors)?result.errors.length:Number(result.errors || 0);notice('资料已刷新：'+(result.documents ?? 0)+' 份文件，'+(result.pages ?? 0)+' 页内容。'+(errors?' 有 '+errors+' 项无法读取。':''),errors>0);loadState();loadSources();}catch(error){notice(error.message,true);}finally{$('reindex').disabled=false;$('reindex').textContent='刷新资料';}
});
$('close-source').addEventListener('click',()=>$('source-dialog').close());
$('source-dialog').addEventListener('click',event=>{if(event.target===$('source-dialog')){const rect=$('source-dialog').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('source-dialog').close();}});
function updateLocks() {
  const locked=busy||studyBusy;
  ['course','send','new-chat','easy','plan-retry'].forEach(id=>$(id).disabled=locked);
  ['question','web','url'].forEach(id=>$(id).disabled=busy);
  $('start').disabled=locked||!plan;
  document.querySelectorAll('[name="minutes"]').forEach(n=>n.disabled=locked);
  $('study-panel').querySelectorAll('button,textarea').forEach(n=>n.disabled=locked);
}
function draftFor(id) { try{return JSON.parse(localStorage.getItem('study-draft:'+id)||'{}');}catch(_){return {};} }
function saveDraft(id,value) { try{localStorage.setItem('study-draft:'+id,JSON.stringify(value));}catch(_){} }
function renderPlan() {
  if(!plan)return;
  $('next-step').textContent=plan.title || plan.next_step || '从一小步开始';
  $('reminder-body').textContent=plan.nudge || '先看一段资料，用自己的话说说你理解了什么。';
  $('plan-context').textContent=[plan.course,plan.context_label,studyRun&&studyRun.status!=='completed'?'已有未结束的小步：'+studyRun.minutes+'分钟，点击继续会恢复它。':'开始后会带上相关资料和停点。'].filter(Boolean).join(' · ');
  $('start').textContent=studyRun&&studyRun.status!=='completed'?'继续这次小步 ↗':'开始这次小步 ↗';
}
async function loadPlan() {
  const request=++planRequest,selected=course;
  $('plan-retry').hidden=true;$('start').disabled=true;
  try {
    const result=await api(query('/api/study/plan',{course:selected,minutes,easy:easy?'1':'0'}));
    if(request!==planRequest||selected!==course)return;
    plan=result.plan;
    if(!detached)studyRun=result.run || null;
    renderPlan();renderRun();if(activeView==='learn')loadHistory();
  }catch(error){if(request===planRequest&&selected===course){plan=null;notice(error.message,true);$('plan-retry').hidden=false;}}
  finally{if(request===planRequest)updateLocks();}
}
async function startStudy() {
  if(busy||studyBusy||!plan)return;
  studyBusy=true;updateLocks();const selected=course;
  let result;
  try {
    result=await api('/api/study/start',{course:plan.course || course,minutes,easy,idempotency_key:key()});
    if(selected!==course)return;
    studyRun=result.run;detached=false;
    if(studyRun.chat_session_id){sessions.set(studyRun.course,studyRun.chat_session_id);saveSessions();}
    renderPlan();renderRun();showView('learn',true);
  }catch(error){notice(error.message,true);}
  finally{studyBusy=false;updateLocks();}
  if(!result?.run)return;
  if(result.resumed){await loadHistory();notice('已接回这次小步，按原来的计划继续。');}
  else if(state?.model?.available){await loadHistory();await sendQuestion(studyRun.prompt,true);}
  else{$('question').value=studyRun.prompt || ''; $('chat-status').textContent='本地模型暂未就绪。可以先阅读上面的原文，小测可选，也可以今天到这；原问题已保留。';}
}
function runButton(label,action,cls='secondary') {const b=el('button',cls,label);b.type='button';b.addEventListener('click',action);return b;}
function completionText(run) {
  if(run.finish_mode==='note'||run.quiz_skipped){
    if(run.stop_note?.trim())return '这次小步已结束，停点笔记已保存。';
    return run.answers?'这次小步已结束，已提交的原回答和参考反馈仍保留。':'这次小步已结束；没有新增学习记录。';
  }
  return '这次作答和你填写的卡点已保存。';
}
function renderStopForm(panel,run) {
  const draft=draftFor(run.id),section=el('div','stop-section'),form=el('form','stop-form');form.hidden=!draft.stopOpen;
  const toggle=runButton('今天到这',()=>{const current=draftFor(run.id);current.stopOpen=!current.stopOpen;saveDraft(run.id,current);form.hidden=!current.stopOpen;toggle.setAttribute('aria-expanded',String(current.stopOpen));if(current.stopOpen)input.focus();});
  toggle.setAttribute('aria-expanded',String(!form.hidden));toggle.setAttribute('aria-controls','stop-form');form.id='stop-form';
  const label=el('label','','学到哪里／哪里卡住（可选）'),input=el('textarea');input.rows=2;input.maxLength=1000;input.value=draft.stopNote || '';input.placeholder='想留就写一句，也可以直接结束。';label.append(input);
  const submit=el('button','primary',input.value.trim()?'保存并结束':'结束这次小步');submit.type='submit';
  input.addEventListener('input',()=>{const current=draftFor(run.id);current.stopNote=input.value;saveDraft(run.id,current);submit.textContent=input.value.trim()?'保存并结束':'结束这次小步';});
  form.append(label,el('p','hint','不需要做小测，也不需要填写笔记。未提交的小测草稿只留在这台浏览器，不会作为作答记录。'),submit);
  form.addEventListener('submit',event=>{event.preventDefault();studyAction('/api/study/finish',{id:run.id,skip_quiz:true,stop_note:input.value,stuck_point:''},'正在结束这次小步…');});section.append(toggle,form);panel.append(section);
}
function renderRun() {
  const panel=$('study-panel'),run=studyRun;panel.replaceChildren();panel.hidden=!run;
  if(!run)return;
  panel.append(el('div','eyebrow',run.course+' · 计划 '+run.minutes+' 分钟'),el('h2','',run.title || '这次的小步'));
  if(run.status!=='completed')renderStopForm(panel,run);
  if(run.context){const detail=el('details','study-excerpt');detail.append(el('summary','',run.context_label || '读回上次的线索'),el('p','study-context',run.context));panel.append(detail);}
  if(run.previous_question)panel.append(el('p','study-context','上次的问题：'+run.previous_question));
  if(run.steps?.length){const list=el('ol','study-steps');run.steps.forEach(step=>list.append(el('li','',step)));panel.append(list);}
  if(run.sources?.length){panel.append(el('h3','','这次用到的资料'),sourceChips(run.sources));run.sources.forEach(source=>{if(source.excerpt){const details=el('details','study-excerpt');details.append(el('summary','',source.title || '资料摘录'),el('p','',source.excerpt));panel.append(details);}});}
  else panel.append(el('p','hint','这一步暂时没有匹配的资料。可以先整理问题，或到“资料”查找依据。'));
  const progress=el('div','study-progress');panel.append(progress);
  if(run.status==='active'){
    progress.append(el('p','hint','小测可选：想检查理解时，可以试试几个小问题；也可以今天到这。'),runButton('试试小测（可选）',()=>studyAction('/api/study/check',{id:run.id},'正在准备小问题，本地模型可能需要几分钟…')));
  }else if(run.status==='quiz'&&!run.answers){
    const form=el('form','quiz-form'),draft=draftFor(run.id);
    form.append(el('h3','','小测（可选）· 用自己的话回答'),el('p','hint',run.quiz?.note || '想不起来也可以如实写下，先留下你现在的理解。'),el('p','hint','可以随时用上方“今天到这”结束，不必提交回答。'));
    (run.quiz?.questions || []).forEach(q=>{const label=el('label','quiz-question',q.question),input=el('textarea');input.rows=3;input.maxLength=2000;input.required=true;input.dataset.questionId=q.id;input.value=draft.answers?.[q.id] || '';label.append(input);form.append(label);input.addEventListener('input',()=>{const current=draftFor(run.id);current.answers={...current.answers,[q.id]:input.value};saveDraft(run.id,current);});});
    const submit=el('button','primary','保存原回答并查看参考反馈');submit.type='submit';form.append(submit);
    form.addEventListener('submit',event=>{event.preventDefault();const answers=[...form.querySelectorAll('textarea')].map(n=>({id:n.dataset.questionId,answer:n.value}));if(!answers.length)return;
      const current=draftFor(run.id),signature=JSON.stringify(answers);if(current.signature!==signature){current.signature=signature;current.answerKey=key();saveDraft(run.id,current);}
      studyAction('/api/study/answer',{id:run.id,answers,idempotency_key:current.answerKey},'正在保存原回答并准备参考反馈…');});progress.append(form);
  }
  if(run.answers){
    progress.append(el('h3','','你保存的原回答'));
    const answers=Array.isArray(run.answers)?run.answers:run.answers.answers || [];
    answers.forEach(answer=>{const item=el('article','saved-answer'),q=run.quiz?.questions?.find(q=>q.id===answer.id);item.append(el('h4','',q?.question || answer.id),el('p','',answer.answer));progress.append(item);});
    progress.append(el('h3','','AI 参考反馈'),el('p','hint','这是对本次回答的参考意见，是否卡住由你自己确认。'));
    if(run.feedback?.status==='ready'){
      (run.feedback.items || []).forEach(item=>{const q=run.quiz?.questions?.find(q=>q.id===item.id);const card=el('article','feedback-item');if(q)card.append(el('h4','',q.question));card.append(markdownText(item.comment || ''));if(item.evidence_quote)card.append(el('blockquote','feedback-evidence','原文依据：'+item.evidence_quote));const source=run.sources?.find(s=>s.id===item.source_id);if(source)card.append(sourceChips([source]));progress.append(card);});if(run.feedback.summary)progress.append(markdownText(run.feedback.summary));
    }else progress.append(el('p','hint','原回答已保存，参考反馈暂时不可用。可以重试，也可以直接确认这次的卡点。'),runButton('重试参考反馈',()=>studyAction('/api/study/feedback',{id:run.id},'正在重新准备参考反馈…')));
    if(run.status!=='completed'){
      const form=el('form','finish-form'),label=el('label','','这次还有哪里卡住？（可选）'),input=el('textarea'),draft=draftFor(run.id);input.rows=2;input.maxLength=1000;input.placeholder='写下你确认的卡点；留空表示这次没有要记录的卡点。';input.value=draft.stuck || '';label.append(input);form.append(label,el('p','hint','保存本次原回答和你确认的卡点，供下次接着学。'));
      input.addEventListener('input',()=>{const current=draftFor(run.id);current.stuck=input.value;saveDraft(run.id,current);});const submit=el('button','primary','确认并保存这次作答');submit.type='submit';form.append(submit);form.addEventListener('submit',event=>{event.preventDefault();studyAction('/api/study/finish',{id:run.id,stuck_point:input.value},'正在保存你确认的记录…');});progress.append(form);
    }
  }
  if(run.status==='completed'){
    progress.append(el('p','completion',completionText(run)));
    if(run.stop_note)progress.append(el('p','study-context','你留下的停点：'+run.stop_note));
    if(run.stuck_point)progress.append(el('p','study-context','你填写的卡点：'+run.stuck_point));
    if(run.quiz_skipped&&Object.values(draftFor(run.id).answers || {}).some(value=>String(value).trim()))progress.append(el('p','hint','未提交的小测草稿仍保留在这台浏览器，未计入作答记录。'));
    progress.append(el('p','hint','结束或保存记录不代表已经掌握。'),runButton('再来一个小步',async()=>{studyRun=null;detached=false;renderRun();showView('today',true);await loadPlan();}));
  }
  updateLocks();
}
async function studyAction(path,body,label) {
  if(busy||studyBusy)return;
  const id=studyRun?.id,selected=course;if(!id)return;
  studyBusy=true;updateLocks();notice(label);
  try{
    const result=await api(path,body,220000);if(selected!==course||studyRun?.id!==id)return;
    studyRun=result.run;renderRun();notice(path.endsWith('/finish')?completionText(studyRun):'');
    if(path.endsWith('/finish')){if(!body.skip_quiz){try{localStorage.removeItem('study-draft:'+id);}catch(_){}} await loadState();detached=true;await loadPlan();detached=false;}
  }catch(error){
    notice(error.message+' 可以重试；填写的草稿仍保留在这台浏览器。',true);
    try{const result=await api(query('/api/study/run',{id}));if(selected===course&&studyRun?.id===id){studyRun=result.run;renderRun();}}catch(_){}
  }finally{studyBusy=false;updateLocks();}
}
$('start').addEventListener('click',startStudy);
$('plan-retry').addEventListener('click',loadPlan);
$('easy').addEventListener('click',()=>{easy=true;minutes=5;document.querySelectorAll('[name="minutes"]').forEach(n=>n.checked=n.value==='5');loadPlan();});
document.querySelectorAll('[name="minutes"]').forEach(n=>n.addEventListener('change',()=>{minutes=Number(n.value);easy=false;loadPlan();}));
async function initialize() {
  const launch=new URLSearchParams(location.search), requested=launch.get('study');
  if(['5','30'].includes(requested)){minutes=Number(requested);easy=launch.get('easy')==='1';if(easy)minutes=5;}
  document.querySelectorAll('[name="minutes"]').forEach(n=>n.checked=Number(n.value)===minutes);
  if(pendingChat?.key && pendingChat.payload?.message){course=pendingChat.payload.course || '';busy=true;restoringChat=true;updateLocks();}else pendingChat=null;
  await loadState();if(!state){if(pendingChat){showView('learn');beginWaiting(true);pauseJob(new Error('暂时连接不到本地服务。'));}return;}await loadPlan();
  if(pendingChat){
    showView('learn');resetChat();
    try{const job=await api(query('/api/chat/job',{id:pendingChat.key}));if(job.status==='completed'){finishJob(job);}else{beginWaiting(true);applyJob(job);}}
    catch(error){beginWaiting(true);pauseJob(error,error.status===404);}
    return;
  }
  if(['5','30'].includes(requested)&&plan){history.replaceState(null,'',location.pathname+location.hash);await startStudy();}
}
initialize();
