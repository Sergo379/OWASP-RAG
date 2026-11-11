'use strict';

const evalView = {
  info: null,
  run: '',
  question: '',
  pending: false,
  lastRevision: '',
  token: 0,
};

function evalOption(value, label) {
  const option = element('option', label);
  option.value = value;
  return option;
}

function evalOptions(id, items, previous) {
  const select = $(id);
  select.replaceChildren(...items.map(([value, label]) => evalOption(value, label)));
  if (items.some(([value]) => value === previous)) select.value = previous;
}

function evalError(err) {
  const box = $('eval-error');
  box.textContent = err ? err.message : '';
  box.classList.toggle('hidden', !err);
}

function evalSuiteOptions() {
  const suite = $('eval-suite').value;
  $('eval-parent-label').classList.toggle('hidden', suite !== 'threshold');
  $('eval-group-label').classList.toggle('hidden', !['retrieval', 'final'].includes(suite));
  const groups = suite === 'retrieval'
    ? ['chunking', 'size', 'overlap', 'topk', 'reranking']
    : suite === 'final' ? ['final_retrieval', 'final_generation'] : [];
  evalOptions('eval-group', [['', 'Все'], ...groups.map(x => [x, x])], '');
  evalUpdateButtons();
}

function evalUpdateButtons() {
  const runs = evalView.info?.runs || [];
  const running = runs.some(item => ['running', 'queued'].includes(item.state.status));
  const chosen = runs.find(item => item.id === evalView.run);
  const canStop = chosen && ['running', 'queued'].includes(chosen.state.status);
  const suite = $('eval-suite').value;
  $('eval-launch').disabled = evalView.pending || running
    || (suite === 'final' && !evalView.info?.frozen)
    || (suite === 'threshold' && !$('eval-parent').value);
  $('eval-stop').disabled = evalView.pending || !canStop;
  $('eval-resume').disabled = evalView.pending || running || !chosen;
  $('eval-charts').disabled = evalView.pending || !chosen || !chosen.state.saved;
}

function evalRenderDashboard(data) {
  const runs = data.runs;
  const counters = [
    ['Development', data.splits.development], ['Final', data.splits.final],
    ['Запросы сохранены', runs.reduce((n, run) => n + (run.state.saved || 0), 0)],
    ['Запросы в очереди', runs.reduce((n, run) => n + Math.max(0, run.planned - (run.state.saved || 0)), 0)],
    ['Ошибки', runs.reduce((n, run) => n + (run.state.failures || 0), 0)],
  ];
  $('eval-summary').replaceChildren(...counters.map(([label, number]) => {
    const block = element('div', undefined, 'eval-stat');
    block.append(element('strong', number), element('span', label));
    return block;
  }));
  if (!$('eval-suite').options.length) {
    evalOptions('eval-suite', data.suites.map(x => [x, x]), '');
    evalSuiteOptions();
  }
  evalOptions('eval-parent', [['', 'Выберите run'], ...runs.filter(x => x.suite === 'retrieval' && x.state.status === 'completed')
    .map(x => [x.id, x.id.slice(0, 8) + ' · ' + date(x.created_at)])], $('eval-parent').value);
  evalOptions('eval-run', [['', 'Выберите run'], ...runs.map(x =>
    [x.id, x.suite + ' · ' + x.id.slice(0, 8) + ' · ' + x.state.status])], evalView.run);
  if (!evalView.run && runs.length) $('eval-run').value = runs[0].id;
  evalView.run = $('eval-run').value;
  const active = runs.find(x => ['running', 'queued'].includes(x.state.status));
  const state = active?.state;
  const operation = state?.current;
  $('eval-operation').textContent = active
    ? active.suite + ' · ' + (state.saved || 0) + '/' + active.planned + ' · ' +
      [operation?.configuration, operation?.question_id, operation?.current,
        operation?.batch ? operation.batch + '/' + operation.batches : null].filter(Boolean).join(' · ')
    : data.frozen ? 'Конфигурация final зафиксирована.' : 'Для final сначала зафиксируйте конфигурацию через eval-freeze.';
  $('eval-report').classList.toggle('hidden', !data.published_report);
  evalUpdateButtons();
}

function evalMetric(record, field, statistic = 'mean', digits = 3) {
  const value = record.metrics[field]?.[statistic];
  return typeof value === 'number' ? value.toFixed(digits) : '—';
}

function evalTable(data) {
  const table = element('table');
  const head = element('thead');
  const headers = element('tr');
  for (const name of ['Конфигурация', 'Запросы', 'Recall', 'Precision', 'MRR', 'Hit Rate', 'Цитаты', 'Отказы', 'Медиана, мс', 'Размечено']) {
    headers.append(element('th', name));
  }
  head.append(headers);
  const body = element('tbody');
  for (const [label, record] of Object.entries(data.configurations)) {
    const row = element('tr');
    const title = element('td', label);
    title.title = JSON.stringify(record.parameters);
    row.append(title);
    const values = [
      record.n + '/' + record.planned, ...['recall', 'precision', 'mrr', 'hit_rate', 'citation_validity', 'refusal_accuracy']
        .map(key => evalMetric(record, key)),
      evalMetric(record, 'latency_ms', 'median', 0), record.reviewed,
    ];
    row.append(...values.map(value => element('td', value)));
    body.append(row);
  }
  table.append(head, body);
  const summary = element('details');
  summary.append(element('summary', 'Параметры, разрезы и ресурсы'),
    element('pre', JSON.stringify({configurations: data.configurations, indexes: data.indexes}, null, 2)));
  $('eval-results').replaceChildren(table, summary);
}

async function evalRefreshRun(force = false) {
  const id = evalView.run;
  for (const kind of ['json', 'csv']) {
    const link = $('eval-' + kind);
    link.classList.toggle('hidden', !id);
    if (id) link.href = '/api/evaluation/runs/' + id + '/export/' + kind;
  }
  if (!id) {
    for (const name of ['eval-results', 'eval-inspector', 'eval-question', 'eval-figures']) $(name).replaceChildren();
    return;
  }
  const item = evalView.info?.runs.find(x => x.id === id);
  const revision = id + ':' + (item?.state?.saved || 0) + ':' + (item?.state?.status || '');
  if (!force && revision === evalView.lastRevision) return;
  const data = await api('/evaluation/runs/' + id);
  if (id !== evalView.run) return;
  evalView.lastRevision = revision;
  evalTable(data);
  evalOptions('eval-question', [['', 'Выберите сохранённый запрос'], ...data.tasks.map(task =>
    [task.key, task.question_id + ' · ' + task.label + ' · ' + task.status])], evalView.question);
  if (!$('eval-question').value) $('eval-inspector').replaceChildren();
  const figures = $('eval-figures');
  figures.replaceChildren(...data.charts.map(name => {
    const details = element('details');
    const src = '/api/evaluation/runs/' + id + '/charts/' + encodeURIComponent(name);
    const img = element('img');
    img.src = src; img.loading = 'lazy'; img.alt = name.replace('.svg', '');
    const download = element('a', 'Скачать SVG');
    download.href = src; download.download = name;
    details.append(element('summary', img.alt), img, download);
    return details;
  }));
}

function evalDetails(label, value) {
  const details = element('details');
  details.append(element('summary', label), element('pre', typeof value === 'string' ? value : JSON.stringify(value ?? null, null, 2)));
  return details;
}

function evalEvidence(question) {
  const details = element('details');
  details.append(element('summary', 'Ground truth и точные фрагменты'));
  if (!question.answerable) {
    details.append(element('p', question.absence.reason),
      element('small', 'Проверено документов: ' + question.absence.searched_documents +
        '. Совпадения маркеров: ' + question.absence.hits + '.'));
    return details;
  }
  for (const evidence of question.source_evidence) {
    const block = element('article', undefined, 'eval-evidence');
    const title = element('a', evidence.title + ' · ' + evidence.section);
    title.href = evidence.url;
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
    const claim = question.expected_claims.find(item => item.id === evidence.claim_id);
    block.append(title, element('p', claim?.text || ''),
      element('small', 'Строка ' + evidence.line_start + ' · snapshot ' + evidence.source_revision.slice(0, 12)),
      element('pre', evidence.quote));
    details.append(block);
  }
  return details;
}

function evalChunks(label, rows) {
  const details = element('details');
  details.append(element('summary', label + ' · ' + rows.length));
  const list = element('ol');
  for (const row of rows) {
    const score = typeof row.vector_similarity === 'number' ? ' · cosine ' + row.vector_similarity.toFixed(3) : '';
    const item = element('li', (row.source_id ? '[' + row.source_id + '] ' : '') +
      row.title + ' · ' + (row.section || 'без раздела') + score);
    const content = row.original_text || row.text;
    if (row.prompt_text) item.append(evalDetails('Текст для модели', row.prompt_text));
    if (content && content !== row.prompt_text) item.append(evalDetails('Исходный фрагмент', content));
    list.append(item);
  }
  details.append(list);
  return details;
}

function evalReviewForm(row, rubric, review, endpoint) {
  const form = element('form');
  form.append(element('h3', 'Ручная оценка'));
  const fields = Object.entries(rubric).map(([field, max]) => {
    const label = element('label', field + ' (0–' + max + ') ');
    const select = element('select');
    select.required = true;
    select.append(evalOption('', '—'), ...Array.from({length:max+1}, (_, i) => evalOption(String(i), String(i))));
    select.value = review ? String(review.scores[field]) : '';
    label.append(select); form.append(label);
    return [field, select];
  });
  const reviewerLabel = element('label', 'Проверяющий ');
  const reviewer = element('input'); reviewer.required = true; reviewer.maxLength = 100;
  reviewer.value = review?.reviewer || '';
  reviewerLabel.append(reviewer);
  const notesLabel = element('label', 'Комментарий с опорой на источники ');
  const notes = element('textarea'); notes.required = true; notes.maxLength = 4000;
  notes.value = review?.notes || '';
  notesLabel.append(notes);
  form.append(reviewerLabel, notesLabel, element('button', 'Сохранить оценку'));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    await evalAction(async () => {
      await api(endpoint + '/review', evalPost({
        reviewer: reviewer.value, notes: notes.value,
        scores: Object.fromEntries(fields.map(([key, select]) => [key, Number(select.value)])),
      }));
      await evalInspect();
    });
  });
  return form;
}

async function evalInspect() {
  const key = $('eval-question').value;
  evalView.question = key;
  const target = $('eval-inspector');
  target.replaceChildren();
  if (!key) return;
  const endpoint = '/evaluation/runs/' + evalView.run + '/questions/' + encodeURIComponent(key);
  const token = ++evalView.token;
  const data = await api(endpoint);
  if (token !== evalView.token || key !== $('eval-question').value) return;
  const q = data.question, r = data.row;
  target.append(element('h3', q.id + ' · ' + q.question), element('p', q.reference_answer),
    element('p', [r.status, r.trace_id ? 'Trace ID ' + r.trace_id : ''].filter(Boolean).join(' · ')));
  if (r.answer) target.append(element('h4', 'Ответ модели'), element('pre', r.answer));
  if (r.error) target.append(element('p', r.error));
  const retrieved = r.retrieved || r.retrieval?.candidate_pool || [];
  const cited = (r.citations || []).flatMap(group => group.chunks.map(chunk => ({...chunk, title: group.title})));
  target.append(evalEvidence(q),
    evalChunks(r.retrieved ? 'Результаты Top-K' : 'Кандидаты поиска', retrieved),
    evalChunks('Выбранный контекст', r.selected_context || []),
    evalChunks('Процитированные фрагменты', cited),
    evalDetails('Цитаты и проверка', {citations:r.citations, validation:r.validation, scores:r.scores, review:data.review}),
    evalDetails('Полный результат запроса', r));
  if (r.status === 'completed' && r.parameters.llm) target.append(evalReviewForm(r, data.rubric, data.review, endpoint));
}

function evalPost(data) {
  return {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)};
}

async function evalLoad(force = false) {
  if (location.hash !== '#experiments') return;
  evalView.info = await api('/evaluation');
  evalRenderDashboard(evalView.info);
  await evalRefreshRun(force);
}

async function evalAction(work) {
  if (evalView.pending) return;
  evalView.pending = true;
  evalUpdateButtons();
  try {
    evalError(null);
    await work();
    await evalLoad(true);
  } catch (error) { evalError(error); }
  finally { evalView.pending = false; evalUpdateButtons(); }
}

$('eval-refresh').onclick = () => evalAction(async () => {});
$('eval-suite').onchange = evalSuiteOptions;
$('eval-parent').onchange = evalUpdateButtons;
$('eval-run').onchange = () => {
  evalView.run = $('eval-run').value;
  evalView.question = '';
  evalView.token += 1;
  $('eval-inspector').replaceChildren();
  evalUpdateButtons();
  evalRefreshRun(true).catch(evalError);
};
$('eval-question').onchange = () => evalInspect().catch(evalError);
$('eval-start').onsubmit = event => {
  event.preventDefault();
  evalAction(async () => {
    const suite = $('eval-suite').value;
    const payload = {suite};
    if (suite === 'threshold') payload.parent = $('eval-parent').value;
    if (['retrieval','final'].includes(suite) && $('eval-group').value) payload.group = $('eval-group').value;
    const run = await api('/evaluation/runs', evalPost(payload));
    evalView.run = run.id; evalView.question = '';
    $('eval-inspector').replaceChildren();
  });
};
$('eval-stop').onclick = () => evalAction(() => api('/evaluation/runs/' + evalView.run + '/stop', evalPost({})));
$('eval-resume').onclick = () => evalAction(() => api('/evaluation/runs/' + evalView.run + '/resume', evalPost({retry_errors:$('eval-retry').checked})));
$('eval-charts').onclick = () => evalAction(() => api('/evaluation/runs/' + evalView.run + '/charts', evalPost({})));
window.addEventListener('hashchange', () => evalLoad().catch(evalError));
setInterval(() => { if (!evalView.pending) evalLoad().catch(evalError); }, 4500);
evalLoad().catch(evalError);
