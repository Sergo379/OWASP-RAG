'use strict';
const $ = id => document.getElementById(id);
let docs = [], stats = {}, lastStatus = '', selected = null, chunkOffset = 0, busy = false;
const names = {dashboard:'Dashboard', knowledge:'Knowledge Base', search:'Search', chat:'AI Chat', experiments:'Experiments'};
function element(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(cls)el.className=cls; return el; }
function date(value) { return value ? new Date(value).toLocaleString('ru-RU') : '—'; }
async function api(path, options) {
  const response = await fetch('/api'+path, {signal:AbortSignal.timeout(15000), ...options});
  const body = await response.json();
  if(!response.ok) throw new Error(typeof body.error==='string' ? body.error : typeof body.detail==='string' ? body.detail : Array.isArray(body.detail) ? body.detail.map(item=>item.loc.join('.')+': '+item.msg).join('; ') : 'Некорректный запрос ('+response.status+')');
  return body;
}
function error(err) { $('connection-error').textContent=err.message || 'Не удалось связаться с сервером'; $('connection-error').classList.remove('hidden'); }
function navigate() {
  const view=location.hash.slice(1)||'dashboard';
  document.querySelectorAll('.page').forEach(el=>el.classList.add('hidden'));
  document.querySelectorAll('nav a').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
  $('breadcrumb').textContent=names[view]||'Dashboard';
  if(view==='dashboard'||view==='knowledge'||view==='search'||view==='chat') $(view).classList.remove('hidden');
  else { $('future').classList.remove('hidden'); $('future-title').textContent=names[view]||'Раздел'; $('future-message').textContent='Будет доступно на этапе №'+(view==='search'?'2':view==='chat'?'3':'4'); }
}
function renderDocs() {
  $('documents').replaceChildren();
  const query=$('filter').value.toLowerCase();
  const filtered=docs.filter(d=>(d.title+' '+d.topic).toLowerCase().includes(query));
  for(const d of filtered) {
    const row=element('tr'), title=element('td'), button=element('button',d.title);
    button.addEventListener('click',()=>openDocument(d));title.append(button);
    const link=element('a','Источник ↗');link.href=d.url;link.target='_blank';link.rel='noreferrer';
    title.append(element('br'),link);row.append(title,element('td',d.topic));
    const status=element('td');status.append(element('span',d.status==='active'?'В корпусе':d.status==='deleted'?'Удалён в источнике':'Вне выборки','badge subtle'));status.append(element('small',d.processed?'Обработан':'Ожидает preprocessing'));
    const version=element('td',d.repository_commit.slice(0,10));version.append(element('small',date(d.fetched_at)));
    row.append(status,element('td',String(d.chunks)),version);$('documents').append(row);
  }
  if(!filtered.length&&docs.length) {const row=element('tr');const cell=element('td','Ничего не найдено');cell.colSpan=5;row.append(cell);$('documents').append(row);}
  $('doc-count').textContent=docs.length;$('empty').classList.toggle('hidden',docs.length>0);
}
async function loadCorpus() {
  [stats, {items:docs}] = await Promise.all([api('/stats'),api('/documents')]);
  $('downloaded').textContent=stats.downloaded_documents;$('processed').textContent=stats.processed_documents;$('chunks').textContent=stats.chunks.toLocaleString('ru-RU');
  $('updated').textContent=stats.last_updated_at?new Date(stats.last_updated_at).toLocaleDateString('ru-RU'):'—';
  $('updated').title=date(stats.last_updated_at);$('commit').textContent=stats.repository_commit?'Commit '+stats.repository_commit.slice(0,12):'Источник ещё не загружен';
  $('active-config').textContent=stats.configuration?`${stats.configuration.strategy} · ${stats.configuration.chunk_size} tokens · ${stats.configuration.overlap*100}% overlap`:'Preprocessing ещё не запускался';
  $('knowledge-summary').textContent=`Активных: ${stats.active_documents} · Обработано: ${stats.processed_documents} · Chunks: ${stats.chunks} · Последнее обновление: ${date(stats.last_updated_at)}`;
  renderDocs();
  const errors=await api('/errors');$('errors').replaceChildren();
  if(!errors.items.length)$('errors').append(element('p','Ошибок в журнале операций нет.','muted'));
  for(const item of errors.items){const row=element('div',item.message,'error-item');row.append(element('small',`${date(item.timestamp)} · ${item.operation}${item.path?' · '+item.path:''}`));$('errors').append(row);}
}
function showOperation(op) {
  busy=op?.status==='running'; $('grab').disabled=busy;$('preprocess').disabled=busy;
  if(!op)return;
  const labels={running:'Выполняется',succeeded:'Завершено',partial:'Завершено с ошибками',failed:'Ошибка',interrupted:'Прервано'};
  $('operation-status').textContent=labels[op.status]||op.status;
  $('operation-text').textContent=`${op.kind} · ${date(op.started_at)} · ${op.completed}/${op.total} · ${op.error||op.current||''}`;
  if(op.total) $('operation-text').textContent += ` · ${(100*op.completed/op.total).toFixed(1)}%`;
  if(op.units==='bytes') $('operation-text').textContent += ` · ${(op.completed/1048576).toFixed(1)}/${(op.total/1048576).toFixed(1)} MiB`;
  if(op.batch) $('operation-text').textContent += ` · batch ${op.batch}/${op.batches}`;
  if(op.elapsed_ms) $('operation-text').textContent += ` · ${(op.elapsed_ms/1000).toFixed(1)} s`;
  if(op.source_chunks) $('operation-text').textContent += ` · исходных chunks: ${op.source_chunks}, обработано: ${op.processed_source_chunks||0}`;
  if(op.excluded_chunks) $('operation-text').textContent += ` · исключено: ${op.excluded_chunks}`;
  $('progress').classList.toggle('hidden',!busy);$('progress').max=op.total||1;$('progress').value=op.completed;
  $('operation-details').classList.remove('hidden');$('operation-result').textContent=JSON.stringify(op,null,2);
}
async function poll() {
  try {
    const state=await api('/state');$('version').textContent='v'+state.version;
    $('components').replaceChildren();for(const [name,status] of Object.entries(state.components)){const row=element('div',name);row.append(element('small',status));$('components').append(row);}
    showOperation(state.operation);
    const key=state.operation?state.operation.operation_id+state.operation.status:'empty';
    if(key!==lastStatus){await loadCorpus();if(window.loadStage2)await window.loadStage2();lastStatus=key;}
    if(window.updateStage2Busy)window.updateStage2Busy();
    if(window.updateChatBusy)window.updateChatBusy();
    $('connection-error').classList.add('hidden');
  }catch(err){error(err);}finally{setTimeout(poll,2000);}
}
async function start(kind) {
  $('grab').disabled=true;$('preprocess').disabled=true;
  try {
    const options={method:'POST',headers:{'Content-Type':'application/json'}};
    if(kind==='preprocess')options.body=JSON.stringify({strategy:$('strategy').value,chunk_size:Number($('chunk-size').value),overlap:Number($('overlap').value)});
    showOperation(await api('/operations/'+kind,options));lastStatus='';
  }catch(err){error(err);$('grab').disabled=busy;$('preprocess').disabled=busy;}
}
async function openDocument(doc) {
  selected=doc;chunkOffset=0;$('viewer-title').textContent=doc.title;
  $('viewer-meta').textContent=`${doc.source_path} · Commit: ${doc.repository_commit} · Скачан: ${date(doc.fetched_at)} · Проверен: ${date(doc.last_checked_at)} · Дата изменения: ${doc.updated_at?date(doc.updated_at):'неизвестна'}`;
  $('viewer-config').replaceChildren();for(const id of stats.available_configurations||[]){const option=element('option',id);option.value=id;option.selected=id===stats.configuration_id;$('viewer-config').append(option);}
  $('viewer').showModal();await showRaw();
}
async function showRaw() {
  $('more-chunks').classList.add('hidden');$('viewer-content').textContent='Загрузка…';
  try {const doc=await api('/documents/'+selected.document_id);$('viewer-content').replaceChildren(element('pre',doc.text));}catch(err){$('viewer-content').textContent=err.message;}
}
async function showChunks(append=false) {
  if(!append){chunkOffset=0;$('viewer-content').replaceChildren();}
  $('more-chunks').disabled=true;
  try {
    const id=$('viewer-config').value;
    const data=await api(`/documents/${selected.document_id}/chunks?offset=${chunkOffset}&limit=20${id?'&configuration_id='+encodeURIComponent(id):''}`);
    if(!append&&data.stale)$('viewer-content').append(element('p','Эти chunks относятся к предыдущей версии документа. Повторите preprocessing.','alert'));
    if(!append&&!data.total)$('viewer-content').append(element('p','Chunks ещё не созданы для этой конфигурации.','muted'));
    for(const chunk of data.items){const card=element('article',undefined,'chunk-card');card.append(element('small',`#${chunk.chunk_index} · ${chunk.token_count} tokens · ${chunk.heading_path.join(' / ')}`),element('pre',chunk.text));const detail=element('details');detail.append(element('summary','Metadata'),element('pre',JSON.stringify({...chunk,text:undefined},null,2)));card.append(detail);$('viewer-content').append(card);}
    chunkOffset+=data.items.length;$('more-chunks').classList.toggle('hidden',chunkOffset>=data.total);
  }catch(err){$('viewer-content').append(element('p',err.message,'alert'));}finally{$('more-chunks').disabled=false;}
}
$('grab').addEventListener('click',()=>start('grab'));$('preprocess').addEventListener('click',()=>start('preprocess'));
$('filter').addEventListener('input',renderDocs);$('strategy').addEventListener('change',()=>{$('overlap').disabled=$('strategy').value!=='overlap';if($('overlap').disabled)$('overlap').value='0';});
$('close-viewer').addEventListener('click',()=>$('viewer').close());$('show-raw').addEventListener('click',showRaw);$('show-chunks').addEventListener('click',()=>showChunks());$('more-chunks').addEventListener('click',()=>showChunks(true));$('viewer-config').addEventListener('change',()=>showChunks());
window.addEventListener('hashchange',navigate);navigate();poll();
