'use strict';
let chatSession = null, chatRunning = false, chatSessionChanging = false, chatRequestId = null, chatController = null, llmState = null;
const jsonPost = body => ({method:'POST', headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});

function inlineMarkdown(parent, text, requestId) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[S\d+\])/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const value = match[0];
    if (value.startsWith('`')) parent.append(element('code', value.slice(1,-1)));
    else if (value.startsWith('**')) parent.append(element('strong', value.slice(2,-2)));
    else { const link=element('a',value); link.href='#source-'+requestId+'-'+value.slice(1,-1); link.addEventListener('click',event=>{event.preventDefault();const target=document.getElementById(link.hash.slice(1));if(target){target.open=true;target.scrollIntoView({block:'center',behavior:'smooth'});}});parent.append(link); }
    offset = match.index+value.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function safeMarkdown(target, text, requestId) {
  target.replaceChildren();
  let inCode=false, code=[], list=null;
  for(const line of text.split('\n')) {
    if(line.trim().startsWith('```')) {
      if(inCode){target.append(element('pre',code.join('\n'),'answer-code'));code=[];}
      inCode=!inCode;list=null;continue;
    }
    if(inCode){code.push(line);continue;}
    if(!line.trim()){list=null;continue;}
    const item=line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/), heading=line.match(/^#{1,4}\s+(.*)/);
    let node;
    if(item){if(!list){list=element('ul');target.append(list);}node=element('li');list.append(node);}
    else {list=null;node=element(heading?'h3':'p');target.append(node);}
    inlineMarkdown(node,item?item[1]:heading?heading[1]:line,requestId);
  }
  if(inCode)target.append(element('pre',code.join('\n'),'answer-code'));
}

function sourceCards(container, sources, requestId) {
  for(const source of sources||[]) {
    const group=element('section',undefined,'citation-group'), link=element('a',source.title+' ↗');
    try{const url=new URL(source.url);if(url.protocol==='https:'&&['github.com','cheatsheetseries.owasp.org'].includes(url.hostname))link.href=url.href;}catch{}
    link.target='_blank';link.rel='noreferrer noopener';group.append(link);
    for(const chunk of source.chunks){const details=element('details');details.id='source-'+requestId+'-'+chunk.source_id;details.append(element('summary',`[${chunk.source_id}] ${chunk.section||'OWASP'} · similarity ${score(chunk.vector_similarity)} · rerank ${score(chunk.rerank_score)}`),element('small',chunk.chunk_id),element('pre',chunk.original_text));group.append(details);}
    container.append(group);
  }
}

function messageCard(message) {
  const card=element('article',undefined,'panel chat-message '+message.role), status=element('small',message.role==='user'?'Вы':'Qwen3 · '+message.status), content=element('div',undefined,'answer-body');
  card.append(status,content);safeMarkdown(content,message.content||'',message.request_id);
  if(message.error)card.append(element('p',message.error,'alert'));
  if(message.role==='assistant'){
    if(['cancelled','failed','interrupted','running'].includes(message.status))card.append(element('p','Частичный ответ · не подтверждён','muted'));
    sourceCards(card,message.sources,message.request_id);
    const details=element('details'), summary=element('summary','Request '+message.request_id+' · Execution details'), pre=element('pre');details.append(summary,pre);
    details.addEventListener('toggle',async()=>{if(details.open){try{const result=await api('/chat/requests/'+message.request_id);pre.textContent=JSON.stringify(result.diagnostics,null,2);if(result.trace_url&&!details.querySelector('a')){const link=element('a','Open in Langfuse ↗');link.href=result.trace_url;link.target='_blank';link.rel='noreferrer';details.append(link);}}catch(err){pre.textContent=err.message;}}});card.append(details);
  }
  $('chat-messages').append(card);return {card,content,status};
}

function renderConversation() {
  $('chat-messages').replaceChildren();
  for(const message of chatSession.messages)messageCard(message);
  if(!chatSession.messages.length)$('chat-messages').append(element('p','Новый диалог. Задайте вопрос на русском или английском.','muted'));
}

window.updateChatBusy = () => {
  const ready=llmState?.models?.some(m=>m.name===$('chat-llm').value&&m.ready);
  $('chat-send').disabled=chatRunning||chatSessionChanging||busy||!ready||!$('chat-index').value||!chatSession;
  $('chat-clear').disabled=chatRunning||chatSessionChanging||busy;
  $('chat-new').disabled=chatRunning||chatSessionChanging;
  $('chat-stop').disabled=!chatRunning;
};

function chatIndexes() {
  const matching=vectorIndexes.filter(index=>index.searchable&&index.identity.model===$('chat-model').value);
  fillSelect('chat-index',matching,'index_id',index=>`${index.index_id.slice(0,10)} · ${index.identity.preprocessing_configuration.strategy}/${index.identity.preprocessing_configuration.chunk_size} · ${index.status}`,matching.length?null:'Нет готового индекса');
  fillSelect('chat-topic',(retrievalSettings.topics||[]).map(topic=>({topic})),'topic',item=>item.topic,'Все темы');
  fillSelect('chat-document',retrievalSettings.documents||[],'document_id',item=>item.title,'Все документы');
  window.updateChatBusy();
}

async function chatRefresh() {
  try {
    [llmState] = await Promise.all([api('/llm/status')]);
    if(!$('chat-llm').dataset.initialized){$('chat-llm').value=llmState.config.default_model;$('chat-llm').dataset.initialized='true';}
    const tracing=await api('/observability/status');
    $('llm-status').textContent=llmState.available?`Ollama ${llmState.version} · `+llmState.models.map(m=>`${m.name}: ${m.ready?'Ready':'Not ready / digest mismatch'}`).join(' · '):llmState.error;
    $('llm-config').textContent=JSON.stringify(llmState,null,2);
    $('langfuse-status').textContent=tracing.configured?`Langfuse · ${tracing.cloud_verification} · export ${tracing.last_export} · ${tracing.capture_texts?'masked text opt-in':'metadata only'}`:tracing.message;
    chatIndexes();
  }catch(err){$('chat-progress').textContent=err.message;}
}

async function newConversation() {
  if(chatRunning||chatSessionChanging)return;
  chatSessionChanging=true;window.updateChatBusy();
  try{chatSession=await api('/chat/sessions',jsonPost());localStorage.setItem('owasp-chat-session',chatSession.session_id);renderConversation();}
  finally{chatSessionChanging=false;window.updateChatBusy();}
}

async function settleRequest(requestId) {
  for(;;){const result=await api('/chat/requests/'+requestId);if(result.status!=='running')return result;await new Promise(resolve=>setTimeout(resolve,500));}
}

async function sendChat(event) {
  event.preventDefault();if(chatRunning||chatSessionChanging||!chatSession)return;
  const sessionId=chatSession.session_id;
  chatRunning=true;chatRequestId=null;chatController=new AbortController();window.updateChatBusy();
  const query=$('chat-query').value.trim();
  const body={session_id:sessionId,llm:$('chat-llm').value,prompt_mode:$('chat-prompt').value,retrieval:{query,index_id:$('chat-index').value,model:$('chat-model').value,top_k:Number($('chat-k').value),similarity_threshold:Number($('chat-threshold').value),candidate_pool_size:Number($('chat-pool').value),topic:$('chat-topic').value||null,document_id:$('chat-document').value||null,reranking:$('chat-reranking').checked}};
  messageCard({role:'user',content:query,request_id:'pending'});const draft=messageCard({role:'assistant',status:'running',request_id:'pending'});let answer='';
  $('chat-progress').textContent='Retrieval → context → Ollama. Поток до проверки цитат считается предварительным.';
  try {
    const response=await fetch('/api/chat/stream',{...jsonPost(body),signal:chatController.signal});
    if(!response.ok){const error=await response.json();throw new Error(error.error||JSON.stringify(error.detail));}
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',final=null;
    for(;;){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let boundary;
      while((boundary=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);const eventName=frame.match(/^event: (.+)$/m)?.[1],raw=frame.match(/^data: (.+)$/m)?.[1];if(!raw)continue;const data=JSON.parse(raw);
        if(eventName==='accepted'){chatRequestId=data.request_id;$('chat-progress').textContent='Request '+data.request_id+' · trace '+data.trace_id;}
        if(eventName==='progress')draft.status.textContent=data.stage;
        if(eventName==='token'){answer+=data.text;draft.content.textContent=answer;}
        if(eventName==='final'){final=data;chatRequestId=data.request_id;}
      }
    }
    if(!final&&chatRequestId)final=await settleRequest(chatRequestId);
    if(!final)throw new Error('Поток завершился без final. Обновите историю для проверки статуса.');
    $('chat-progress').textContent=`${final.status} · ${(final.diagnostics.total_ms/1000).toFixed(1)} s · request ${final.request_id} · trace ${final.trace_id}`;
    $('chat-query').value='';
  }catch(err){
    $('chat-progress').textContent=err.name==='AbortError'?'Остановка upstream Ollama…':err.message;
    if(chatRequestId){await api('/chat/requests/'+chatRequestId+'/cancel',jsonPost());const final=await settleRequest(chatRequestId);$('chat-progress').textContent=final.status+' · '+(final.error||'');}
  }finally{
    try{chatSession=await api('/chat/sessions/'+sessionId);renderConversation();}catch(err){$('chat-progress').textContent=err.message;}
    chatRunning=false;chatController=null;window.updateChatBusy();
  }
}

$('chat-form').addEventListener('submit',sendChat);
$('chat-stop').addEventListener('click',()=>{if(chatRequestId)api('/chat/requests/'+chatRequestId+'/cancel',jsonPost()).catch(error);chatController?.abort();});
$('chat-clear').addEventListener('click',async()=>{if(chatRunning||chatSessionChanging||!chatSession)return;chatSessionChanging=true;window.updateChatBusy();try{chatSession=await api('/chat/sessions/'+chatSession.session_id+'/clear',jsonPost());renderConversation();}catch(err){$('chat-progress').textContent=err.message;}finally{chatSessionChanging=false;window.updateChatBusy();}});
$('chat-new').addEventListener('click',()=>newConversation().catch(error));
$('chat-refresh').addEventListener('click',chatRefresh);
$('chat-model').addEventListener('change',chatIndexes);$('chat-llm').addEventListener('change',window.updateChatBusy);
const stage2Load=window.loadStage2;window.loadStage2=async()=>{await stage2Load();chatIndexes();};
(async()=>{try{const saved=localStorage.getItem('owasp-chat-session');if(saved){try{chatSession=await api('/chat/sessions/'+saved);renderConversation();}catch{await newConversation();}}else await newConversation();await chatRefresh();const last=chatSession.messages.at(-1);if(last?.status==='running'){chatRunning=true;chatRequestId=last.request_id;window.updateChatBusy();const result=await settleRequest(chatRequestId);$('chat-progress').textContent=result.status;chatSession=await api('/chat/sessions/'+chatSession.session_id);renderConversation();chatRunning=false;window.updateChatBusy();}}catch(err){$('chat-progress').textContent=err.message;}})();
