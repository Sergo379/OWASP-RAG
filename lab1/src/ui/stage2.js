'use strict';
let vectorIndexes = [], modelStates = [], retrievalSettings = {}, searching = false;
const bytes = value => (value / 1048576).toFixed(1) + ' MiB';
const score = value => value === null || value === undefined ? '—' : Number(value).toFixed(5);

function fillSelect(id, items, valueKey, label, emptyLabel) {
  const select = $(id), previous = select.value;
  select.replaceChildren();
  if (emptyLabel) { const option = element('option', emptyLabel); option.value = ''; select.append(option); }
  for (const item of items) {
    const option = element('option', label(item)); option.value = item[valueKey]; select.append(option);
  }
  if (Array.from(select.options).some(o => o.value === previous)) select.value = previous;
}

window.updateStage2Busy = () => {
  document.querySelectorAll('[data-mutation], #build-index').forEach(button => { button.disabled = busy || searching; });
  $('search-submit').disabled = busy || searching || !$('search-index').value;
};

function updateSearchIndexes() {
  const model = $('search-model').value;
  const matching = vectorIndexes.filter(index => index.searchable && index.identity.model === model);
  fillSelect('search-index', matching, 'index_id', index => `${index.index_id.slice(0,10)} · ${index.identity.preprocessing_configuration.strategy}/${index.identity.preprocessing_configuration.chunk_size} · ${index.status}`, matching.length ? null : 'Нет готового индекса');
  const state = modelStates.find(item => item.key === model);
  $('search-model-status').textContent = state ? `${state.model_identity} · ${state.status} · ${state.max_input_tokens} input tokens${state.loaded_in_memory ? ' · в памяти' : ''}` : 'Загрузка состояния модели…';
  window.updateStage2Busy();
}

async function inspectIndex(indexId) {
  try {
    const item = await api('/indexes/' + indexId);
    $('index-inspection-text').textContent = JSON.stringify(item, null, 2);
    $('index-inspection').classList.remove('hidden'); $('index-inspection').open = true;
  } catch (err) { error(err); }
}

async function mutation(path, body) {
  try {
    showOperation(await api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}));
    lastStatus = ''; window.updateStage2Busy();
  } catch (err) { error(err); }
}

window.loadStage2 = async () => {
  const [models, indexes, settings] = await Promise.all([api('/embeddings/models'), api('/indexes'), api('/retrieval/configuration')]);
  modelStates = models.items; vectorIndexes = indexes.items; retrievalSettings = settings;
  $('model-list').replaceChildren();
  for (const model of modelStates) {
    const card = element('article', undefined, 'model-card');
    card.append(element('strong', model.key === 'reranker' ? 'Cross-encoder Reranker' : model.key === 'e5' ? 'Multilingual E5 Small' : 'Multilingual MiniLM L12'), element('small', model.model_identity));
    card.append(element('p', `${model.status}${model.loaded_in_memory ? ' · в памяти' : ''} · ${bytes(model.disk_bytes)} / ${bytes(model.expected_bytes)}`, 'muted'));
    if(model.error) card.append(element('p', model.error, 'alert'));
    const button = element('button', model.status === 'ready' ? 'Prepare / Verify Model' : 'Prepare Model');
    button.dataset.mutation = 'true'; button.addEventListener('click', () => mutation('/embeddings/prepare', {model:model.key})); card.append(button); $('model-list').append(card);
  }
  fillSelect('build-config', settings.preprocessing, 'configuration_id', run => `${run.configuration.strategy} / ${run.configuration.chunk_size} / ${run.configuration.overlap*100}% · ${run.chunk_count} chunks`);
  if(!$('build-config').dataset.initialized && settings.default_configuration_id) { $('build-config').value = settings.default_configuration_id; $('build-config').dataset.initialized = 'true'; }
  fillSelect('search-topic', settings.topics.map(topic => ({topic})), 'topic', item => item.topic, 'Все темы');
  fillSelect('search-document', settings.documents, 'document_id', item => item.title, 'Все документы');
  $('index-list').replaceChildren();
  for (const index of vectorIndexes) {
    const identity = index.identity, pub = index.published, row = element('tr');
    const id = element('td', index.index_id.slice(0,12)); id.append(element('small', 'Snapshot ' + identity.source_snapshot_fingerprint.slice(0,12)), element('small', 'Commit ' + (identity.source_snapshot.commit||'—').slice(0,12)));
    const model = element('td', identity.model); model.append(element('small', `${identity.preprocessing_configuration.strategy} / ${identity.preprocessing_configuration.chunk_size} / ${identity.preprocessing_configuration.overlap*100}%`), element('small', identity.embedding_configuration.heading_policy));
    const count = element('td', pub ? `${pub.vectors_count} vectors · ${pub.dimension}D` : 'Не опубликован'); count.append(element('small', bytes(index.disk_bytes) + ' · включая поколения'));
    const status = element('td', index.status); status.append(element('small', date(index.created_at)), element('small', pub ? (pub.duration_ms/1000).toFixed(2)+' s · '+pub.indexed_documents+' docs' : '—'));
    const actions = element('td'), inspect = element('button','Inspect Index'), rebuild = element('button', index.searchable ? 'Rebuild Index' : 'Build / Resume');
    inspect.addEventListener('click', () => inspectIndex(index.index_id)); rebuild.dataset.mutation='true';
    rebuild.addEventListener('click', () => mutation('/indexes/build', {model:identity.model, configuration_id:identity.preprocessing_configuration_id, heading_policy:identity.embedding_configuration.heading_policy, overflow_policy:identity.embedding_configuration.overflow_policy, rebuild:index.searchable}));
    actions.append(inspect, element('br'), rebuild); row.append(id, model, count, status, actions); $('index-list').append(row);
  }
  $('index-empty').classList.toggle('hidden', vectorIndexes.length > 0);
  $('retrieval-summary').textContent = `Индексов: ${vectorIndexes.length} · Ready: ${vectorIndexes.filter(i=>i.status==='Ready').length} · Локальных моделей: ${modelStates.filter(m=>m.status==='ready').length}/3 · LLM Generation ещё не подключена.`;
  updateSearchIndexes(); await updateAnalysis(); window.updateStage2Busy();
};

async function updateAnalysis() {
  if(!$('build-config').value) { $('chunk-analysis').textContent = 'Нет preprocessing chunks для анализа.'; return; }
  const analysis = await api('/chunks/analysis?configuration_id=' + $('build-config').value);
  $('chunk-analysis').textContent = `Chunks: ${analysis.total} · Только заголовки: ${analysis.heading_only} · Короткие (<${analysis.short_threshold}): ${analysis.short} (${analysis.short_percent}%) · Tokens min/median/p90/max: ${Object.values(analysis.distribution).join(' / ')}`;
}

function resultCard(result) {
  const card = element('article', undefined, 'panel search-result');
  const heading = element('div', undefined, 'panel-title'); heading.append(element('span','#'+result.final_rank,'rank'), element('h2',result.title), element('span',result.topic,'badge subtle'));
  const scores = `Cosine similarity ${score(result.vector_similarity)} · Distance ${score(result.vector_distance)} · Retrieval rank ${result.retrieval_rank}` + (result.rerank_score===null ? '' : ` · Raw rerank logit ${score(result.rerank_score)}`);
  card.append(heading, element('small',result.heading_path.join(' / '),'muted'), element('p',scores,'score-line'), element('pre',result.text,'result-text'));
  const link = element('a','Открыть источник OWASP ↗'); link.href=result.url; link.target='_blank'; link.rel='noreferrer'; card.append(link, element('p','Chunk '+result.chunk_id,'muted chunk-id'));
  const full = element('details'); full.append(element('summary','Полный исходный chunk и metadata'), element('pre',result.original_text), element('pre',JSON.stringify(result.metadata,null,2))); card.append(full);
  return card;
}

function renderSearch(data) {
  $('search-results').replaceChildren();
  for(const item of data.results) $('search-results').append(resultCard(item));
  $('search-message').textContent = data.results.length ? `Найдено ${data.results.length} из Top-K ${data.diagnostics.parameters.top_k} · ${data.diagnostics.timings.total_ms.toFixed(1)} ms · ${data.diagnostics.candidates_retrieved} candidates` : 'Нет результатов. Уменьшите threshold, снимите фильтры или проверьте актуальность индекса.';
  if(data.diagnostics.warnings.length) $('search-message').append(element('p',data.diagnostics.warnings.join(' '),'alert'));
  $('search-diagnostics').classList.remove('hidden'); $('search-diagnostics-text').textContent=JSON.stringify(data.diagnostics,null,2);
  $('search-comparison').classList.toggle('hidden',!data.diagnostics.reranking_applied);
  $('comparison-table').replaceChildren(); $('comparison-summary').replaceChildren();
  if(data.diagnostics.reranking_applied) {
    for(const [label,results] of [['Without Reranking',data.without_reranking],['With Reranking',data.results]]) {
      const column=element('div'); column.append(element('h3',label));
      for(const result of results) column.append(element('p',`${result.final_rank}. ${result.title} · chunk ${result.chunk_id.slice(0,8)}`));
      $('comparison-summary').append(column);
    }
    for(const item of data.comparison) { const row=element('tr'); for(const value of [item.chunk_id.slice(0,12),item.original_rank,item.new_rank,score(item.vector_similarity),score(item.rerank_score),item.delta]) row.append(element('td',String(value))); $('comparison-table').append(row); }
  }
}

$('build-index').addEventListener('click',()=>mutation('/indexes/build',{model:$('build-model').value,configuration_id:$('build-config').value||null,heading_policy:$('heading-policy').value,overflow_policy:$('overflow-policy').value,rebuild:false}));
$('build-config').addEventListener('change',()=>updateAnalysis().catch(error)); $('search-model').addEventListener('change',updateSearchIndexes);
$('search-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(searching)return;
  searching=true; window.updateStage2Busy(); $('search-submit').textContent='Поиск…'; $('search-message').textContent='Выполняется локальный поиск. Первый запрос включает загрузку weights в CPU; reranking может занять дополнительное время.';
  try {
    const payload={query:$('search-query').value,index_id:$('search-index').value,model:$('search-model').value,top_k:Number($('search-k').value),similarity_threshold:Number($('search-threshold').value),topic:$('search-topic').value||null,document_id:$('search-document').value||null,reranking:$('search-reranking').checked,candidate_pool_size:Number($('candidate-pool').value)};
    renderSearch(await api('/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(180000)}));
  } catch(err) { $('search-message').textContent=err.message; $('search-results').replaceChildren(); $('search-diagnostics').classList.add('hidden'); $('search-comparison').classList.add('hidden'); }
  finally { searching=false; $('search-submit').textContent='Search'; window.updateStage2Busy(); window.loadStage2().catch(error); }
});
window.loadStage2().catch(error);
