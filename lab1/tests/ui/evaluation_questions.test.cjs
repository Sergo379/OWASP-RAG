const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'src/ui/evaluation.js'), 'utf8');
const dataset = JSON.parse(fs.readFileSync(path.join(root, 'experiments/datasets/owasp-v1.json'), 'utf8'));
const questions = new Map(dataset.map(question => [question.id, question]));

function task(question_id, model, label, status, prompt_mode = null) {
  return {question_id, model, label, status, prompt_mode,
    key: label + '__' + question_id, question: questions.get(question_id).question};
}

const tasks = [
  task('OWASP-Q01', 'e5', 'threshold-e5-low', 'completed'),
  task('OWASP-Q01', 'qwen3:1.7b', 'qwen-17', 'completed', 'strict_grounded'),
  task('OWASP-Q01', 'qwen3:4b-instruct-2507-q4_K_M', 'qwen-4-instruct', 'timeout', 'strict_grounded'),
  task('OWASP-Q03', 'minilm', 'topk-minilm-5', 'completed'),
  task('OWASP-Q30', 'e5', 'final-retrieval', 'refused'),
];

function harness() {
  const calls = [];
  const nodes = new Map();
  const document = {activeElement: null};
  let runTasks = tasks;

  class Element {
    constructor(tag, text) {
      this.tag = tag;
      this.textContent = text ?? '';
      this.children = [];
      this.value = '';
      this.dataset = {};
      this.attributes = new Map();
      this.scrollTop = 0;
      const classes = new Set();
      this.classList = {toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
        contains: name => classes.has(name)};
    }
    get options() { return this.children; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) {
      this.children = children;
      if (children[0]?.tag === 'option') this.value = children[0].value;
    }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    querySelectorAll(tag) {
      return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]);
    }
    setAttribute(name, value) { this.attributes.set(name, value); }
    focus() { document.activeElement = this; }
    scrollIntoView() {}
    addEventListener() {}
  }

  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id));
    return nodes.get(id);
  };
  const api = async endpoint => {
    calls.push(endpoint);
    if (endpoint === '/evaluation/runs/test-run') return {configurations: {}, indexes: {}, tasks: runTasks, charts: []};
    const key = decodeURIComponent(endpoint.split('/').at(-1));
    const selected = runTasks.find(candidate => candidate.key === key);
    assert.ok(selected, 'detail request uses the real task key');
    return {
      question: questions.get(selected.question_id),
      row: {status: selected.status, parameters: {}, trace_id: 'test-trace', answer: 'Saved answer'},
    };
  };
  const context = vm.createContext({$, api, document, tasks, window: {addEventListener() {}},
    location: {hash: ''}, setInterval() {}, element: (tag, text) => new Element(tag, text)});
  const evaluate = code => vm.runInContext(code, context);
  evaluate(script);
  evaluate("evalSelectRun('test-run'); evalSetQuestionTasks(tasks);");

  return {
    $, calls, evaluate, document,
    cards: () => $('eval-question-list').querySelectorAll('button'),
    search(value) {
      const input = $('eval-question-search');
      input.value = value;
      input.oninput();
    },
    filter(name, value) {
      const control = $('eval-question-' + name);
      control.value = value;
      control.onchange();
    },
    async refresh(newTasks = tasks) {
      runTasks = newTasks;
      await evaluate('evalRefreshRun(true)');
    },
  };
}

test('finds a full question ID without submitting the page', () => {
  const ui = harness();
  ui.search('OWASP-Q30');
  assert.deepEqual(ui.cards().map(card => card.dataset.key), [tasks[4].key]);
  assert.deepEqual(ui.calls, []);
});

test('finds a partial question ID across configurations', () => {
  const ui = harness();
  ui.search('Q01');
  assert.equal(ui.cards().length, 3);
});

test('searches the actual Russian and English question text', () => {
  const ui = harness();
  ui.search('для совместимости');
  assert.equal(ui.cards()[0].dataset.key, tasks[3].key);
  ui.search('restricted database account');
  assert.equal(ui.cards()[0].dataset.key, tasks[4].key);
  ui.search(tasks[4].question);
  assert.equal(ui.cards()[0].dataset.key, tasks[4].key);
});

test('search ignores case and repeated whitespace', () => {
  const ui = harness();
  ui.search('  АТРИБУТ   SECURE ');
  assert.equal(ui.cards().length, 3);
});

test('model options and cards use saved models', () => {
  const ui = harness();
  const model = ui.$('eval-question-model');
  assert.deepEqual(model.options.map(option => option.value),
    ['', 'e5', 'minilm', 'qwen3:1.7b', 'qwen3:4b-instruct-2507-q4_K_M']);
  ui.filter('model', 'qwen3:1.7b');
  assert.equal(ui.cards()[0].children[1].textContent, 'Qwen3 1.7B · strict_grounded · completed');
});

test('filters by the saved configuration label', () => {
  const ui = harness();
  ui.filter('config', 'threshold-e5-low');
  assert.equal(ui.cards().length, 1);
  assert.equal(ui.cards()[0].children[1].textContent, 'E5 · threshold-e5-low · completed');
});

test('filters by recorded status, including timeout', () => {
  const ui = harness();
  assert.deepEqual(ui.$('eval-question-status').options.map(option => option.value),
    ['', 'completed', 'failed', 'timeout', 'cancelled', 'interrupted', 'refused']);
  ui.filter('status', 'timeout');
  assert.deepEqual(ui.cards().map(card => card.dataset.key), [tasks[2].key]);
});

test('intersects query, model, configuration and status', () => {
  const ui = harness();
  ui.search('cookie');
  ui.filter('model', 'qwen3:4b-instruct-2507-q4_K_M');
  ui.filter('config', 'qwen-4-instruct');
  ui.filter('status', 'completed');
  assert.equal(ui.cards().length, 0);
  ui.filter('status', 'timeout');
  assert.deepEqual(ui.cards().map(card => card.dataset.key), [tasks[2].key]);
});

test('displays an empty state and the total count', () => {
  const ui = harness();
  ui.search('no matching question');
  assert.equal(ui.$('eval-question-empty').classList.contains('hidden'), false);
  assert.equal(ui.$('eval-question-count').textContent, 'Найдено 0 из 5');
});

test('reset clears the query and all three filters', () => {
  const ui = harness();
  ui.search('no matching question');
  ui.filter('model', 'e5');
  ui.filter('config', 'final-retrieval');
  ui.filter('status', 'refused');
  ui.$('eval-question-reset').onclick();
  for (const control of ['search', 'model', 'config', 'status']) {
    assert.equal(ui.$('eval-question-' + control).value, '');
  }
  assert.equal(ui.cards().length, tasks.length);
});

test('clicking a result opens its full detail and highlights the card', async () => {
  const ui = harness();
  ui.search('Q03');
  await ui.cards()[0].onclick();
  assert.deepEqual(ui.calls, ['/evaluation/runs/test-run/questions/' + tasks[3].key]);
  assert.equal(ui.$('eval-inspector').children[0].textContent, 'OWASP-Q03 · ' + tasks[3].question);
  assert.equal(ui.cards()[0].attributes.get('aria-pressed'), 'true');
  assert.equal(ui.document.activeElement, ui.$('eval-inspector').children[0]);
});

test('refresh preserves search, filters, selected card and open detail', async () => {
  const ui = harness();
  ui.search('Q01');
  ui.filter('model', 'e5');
  ui.filter('config', 'threshold-e5-low');
  ui.filter('status', 'completed');
  await ui.cards()[0].onclick();
  const detail = ui.$('eval-inspector').children[0];
  await ui.refresh([...tasks, task('OWASP-Q21', 'e5', 'final-retrieval', 'completed')]);
  assert.deepEqual(ui.cards().map(card => card.dataset.key), [tasks[0].key]);
  assert.equal(ui.cards()[0].attributes.get('aria-pressed'), 'true');
  assert.equal(ui.$('eval-inspector').children[0], detail);
  assert.equal(ui.$('eval-question-search').value, 'Q01');
  assert.equal(ui.$('eval-question-model').value, 'e5');
  assert.equal(ui.$('eval-question-config').value, 'threshold-e5-low');
  assert.equal(ui.$('eval-question-status').value, 'completed');
  assert.equal(ui.$('eval-question-count').textContent, 'Найдено 1 из 6');
  assert.equal(ui.calls.length, 2);
});

test('limits cards to one page and can open the next page', async () => {
  const ui = harness();
  await ui.refresh(Array.from({length: 30}, (_, i) => ({...tasks[0], key: 'test-' + i})));
  assert.equal(ui.cards().length, 24);
  ui.$('eval-question-next').onclick();
  assert.equal(ui.cards().length, 6);
  assert.equal(ui.cards()[0].dataset.key, 'test-24');
  assert.equal(ui.$('eval-question-next').disabled, true);
});
