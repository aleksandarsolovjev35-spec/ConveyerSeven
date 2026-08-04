const fs = require('fs');
const path = require('path');
const {JSDOM} = require('jsdom');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'vision/ui/templates/index.html'), 'utf8');
const scriptPaths = [...html.matchAll(/<script src="\/static\/(js\/[^"?]+)(?:\?[^" ]*)?"><\/script>/g)]
  .map(match => path.join(root, 'vision/ui/static', match[1]));
if (!scriptPaths.length) throw new Error('No production JavaScript modules found');
const roles = [
  'INPUT_LEFT', 'INPUT_RIGHT', 'SPIDER_LEFT', 'SPIDER_RIGHT',
  'SPIDER_IN', 'SPIDER_OUT', 'TOP',
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
const sleep = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const jsonResponse = (payload, status = 200) => new Response(
  JSON.stringify(payload),
  {status, headers: {'content-type': 'application/json'}},
);

function controls(overrides = {}) {
  return {
    start: false,
    stop: false,
    pause: false,
    resume: false,
    exit: true,
    jog_hold: false,
    selected_model_analysis: false,
    selected_model_release: false,
    distributor_diagnostic: false,
    camera_diagnostic: false,
    vision_rule_diagnostic: false,
    ...overrides,
  };
}

// Пороги TOP-камеры для панели «ПОРОГИ ПРАВИЛ». Значения и ревизия
// меняются моком POST /api/thresholds, как настоящий backend.
let thresholdsValues = {min_confidence: 0.4, expected_count: 7, false_positive_max_count: 2};
let thresholdsRevision = 1;

function thresholdsPayload(role, revision, values) {
  return {
    role,
    available: true,
    editable: true,
    revision,
    values: {...values},
    labels: {},
    rules: [
      {
        rule: 'input_window_geometry', label: 'ГЕОМЕТРИЯ ОКНА',
        params: [
          {key: 'min_confidence', label: 'Мин. уверенность', value: values.min_confidence, step: 0.01, min: 0, max: 1},
          {key: 'expected_count', label: 'Ожидаемое число', value: values.expected_count, step: 1, min: 0, max: 1000},
        ],
      },
      {
        rule: 'input_part_presence', label: 'НАЛИЧИЕ КОРПУСА',
        params: [
          {key: 'false_positive_max_count', label: 'Ложных срабатываний', value: values.false_positive_max_count, step: 1, min: 0, max: 100},
        ],
      },
    ],
  };
}

function lineStatus(state, overrides = {}) {
  return {
    state,
    exit_requested: false,
    fault_reason: null,
    step: 0,
    in_line: 0,
    line_parts: [],
    total: 0,
    good: 0,
    rejected: 0,
    cleanup: 0,
    empty: 0,
    dist1_position: 0,
    dist1_max: 340,
    dist1_state: 'IDLE',
    dist2_position: 0,
    dist2_max: 340,
    dist2_state: 'IDLE',
    dist2_target: 'BAD',
    last_distributor_action: '-',
    diagnostic_allowed: false,
    diagnostic_busy: false,
    selected_analysis: {active: false, role: null},
    frame_analysis: {
      available: false, kind: null, active: false,
      models: [], rules: [],
    },
    diagnostics: {
      status: 'NOT_RUN', kind: null, message: 'not run',
      cameras: [], models: [], rules: [], updated_at: null,
    },
    controls: controls(),
    process: {phase: 'IDLE', label: 'Ожидание', positions: [], conveyor: {}},
    jog: {
      active: false,
      can_enter: false,
      busy: false,
      hold_steps: 1000000,
      last_action: '-',
      direction: null,
      error: null,
      live_fps: 30.0,
    },
    ...overrides,
  };
}

async function main() {
  const htmlWithoutScripts = html.replace(
    /<script src="\/static\/js\/[^>]+><\/script>/g,
    '',
  );
  const dom = new JSDOM(htmlWithoutScripts, {
    url: 'http://127.0.0.1:8000/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
  });
  const window = dom.window;
  window.__TRANSPORTER_UI_TEST__ = true;
  window.Response = Response;
  window.console = {...console, log: () => {}, warn: () => {}};

  const calls = [];
  let currentStatus = lineStatus('IDLE');
  let currentMode = 'RULES';
  let statusOffline = false;
  let activeCameraDelay = 0;
  let diagnosticResolver = null;
  let diagnosticFailure = false;
  let startResolver = null;
  let rejectNextStart = false;
  let archiveFound = true;

  window.fetch = async (url, options = {}) => {
    const target = String(url);
    calls.push({url: target, options});
    if (target === '/api/status') {
      if (statusOffline) throw new Error('backend offline');
      return jsonResponse({
        line_status: currentStatus,
        recent_parts: [],
        mode: currentMode,
        frame_version: 1,
        frame_versions: Object.fromEntries(roles.map(role => [role, 1])),
      });
    }
    if (target === '/api/cameras') return jsonResponse({cameras: roles});
    if (target === '/api/boot') {
      return jsonResponse({active: false, progress: 1, steps: [], log: []});
    }
    if (target.startsWith('/api/mode/')) {
      currentMode = target.split('/').pop();
      return jsonResponse({mode: currentMode});
    }
    if (target.startsWith('/api/active_camera/')) {
      if (activeCameraDelay) await sleep(activeCameraDelay);
      return jsonResponse({ok: true});
    }
    if (target === '/api/diagnostics/selected/release') {
      currentStatus.selected_analysis = {active: false, role: null};
      currentStatus.frame_analysis = {
        available: false,
        kind: null,
        active: false,
        models: [],
        rules: [],
      };
      currentStatus.diagnostics = {
        status: 'NOT_RUN', kind: null, message: 'not run',
        cameras: [], models: [], rules: [], updated_at: null,
      };
      currentStatus.controls = controls({
        start: true, exit: true, jog_hold: true,
        selected_model_analysis: true,
        distributor_diagnostic: true,
        camera_diagnostic: true,
        vision_rule_diagnostic: true,
      });
      currentStatus.jog = {...currentStatus.jog, active: true, live_fps: 30.0};
      return jsonResponse({ok: true});
    }
    if (target.startsWith('/api/diagnostics/selected/')) {
      const role = decodeURIComponent(target.split('/').pop());
      currentStatus.selected_analysis = {active: true, role};
      currentStatus.controls = controls({
        exit: true,
        selected_model_release: true,
      });
      currentStatus.diagnostics = {
        status: 'PASSED', kind: 'SELECTED_MODEL', message: `${role}: модели 2, правила 2`,
        selected_role: role,
        cameras: [{role, ok: true, width: 1280, height: 720, detections: 2}],
        models: [
          {role, model: 'weights/a.pt', ok: true, elapsed_ms: 10, detections: 1},
          {role, model: 'weights/b.pt', ok: true, elapsed_ms: 15, detections: 1},
        ],
        rules: [
          {name: 'rule_ok', triggered: false, skipped: false, detail: 'Норма',
            consensus: {runs: 3, required_votes: 2, states: [false, false, true]},
            run_cards: [
              [{role: 'SPIDER_IN', metrics: [
                {label: 'Допуск отклонения уровня контактов', value: '0.5 px', limit: '0.8 px', ok: true, value_raw: 0.5, limit_raw: 0.8},
              ]}],
              [{role: 'SPIDER_IN', metrics: [
                {label: 'Допуск отклонения уровня контактов', value: '0.79 px', limit: '0.8 px', ok: true, value_raw: 0.79, limit_raw: 0.8},
              ]}],
              [{role: 'SPIDER_IN', metrics: [
                {label: 'Допуск отклонения уровня контактов', value: '0.9 px', limit: '0.8 px', ok: false, value_raw: 0.9, limit_raw: 0.8},
              ]}],
            ]},
          {name: 'rule_bad', triggered: true, skipped: false, detail: 'Сработало',
            consensus: {runs: 3, required_votes: 2, states: [true, false, true]},
            run_cards: [
              [{role: 'TOP', metrics: [
                {label: 'Мин. размер лишнего фрагмента, px', value: '12 px', limit: '3 px', ok: false, value_raw: 12, limit_raw: 3},
              ]}],
              [{role: 'TOP', metrics: [
                {label: 'Мин. размер лишнего фрагмента, px', value: '4 px', limit: '3 px', ok: false, value_raw: 4, limit_raw: 3},
              ]}],
              [{role: 'TOP', metrics: [
                {label: 'Мин. размер лишнего фрагмента, px', value: '30 px', limit: '3 px', ok: false, value_raw: 30, limit_raw: 3},
              ]}],
            ]},
        ],
        updated_at: Date.now() / 1000,
      };
      currentStatus.frame_analysis = {
        available: true,
        kind: 'SELECTED',
        active: true,
        title: 'АНАЛИЗ КАДРА',
        role,
        message: currentStatus.diagnostics.message,
        models: currentStatus.diagnostics.models,
        rules: currentStatus.diagnostics.rules,
        picture_run: 2,
        picture_reason: 'rule_bad: Мин. размер лишнего фрагмента, px 4 px (порог 3 px) — брак, ближе всего к порогу',
        updated_at: currentStatus.diagnostics.updated_at,
      };
      return jsonResponse({ok: true});
    }
    if (target === '/api/diagnostics/cameras') {
      currentStatus.diagnostics = {
        status: 'PASSED', kind: 'CAMERAS', message: 'Камеры: 7/7 OK',
        cameras: roles.map(role => ({role, ok: true, width: 1280, height: 720})),
        models: [], rules: [], updated_at: Date.now() / 1000,
      };
      return jsonResponse({ok: true});
    }
    if (target === '/api/diagnostics/vision-rules') {
      currentStatus.diagnostics = {
        status: 'PASSED', kind: 'VISION_RULES', message: 'Модели и rules выполнены',
        cameras: roles.map(role => ({role, ok: true, width: 1280, height: 720, detections: 1})),
        models: [
          {role: 'INPUT_LEFT', model: 'weights/input.pt', ok: true, elapsed_ms: 12, detections: 1},
          {role: 'TOP', model: 'weights/top.pt', ok: true, elapsed_ms: 30, detections: 2},
        ],
        rules: [
          {name: 'rule_ok', triggered: false},
          {name: 'rule_bad', triggered: true},
        ],
        updated_at: Date.now() / 1000,
      };
      return jsonResponse({ok: true});
    }
    if (target.startsWith('/api/distributor/diagnostic/')) {
      if (diagnosticResolver) {
        await new Promise(resolve => { diagnosticResolver = resolve; });
      }
      if (diagnosticFailure) {
        diagnosticFailure = false;
        return jsonResponse({ok: false, error: 'diagnostic rejected'}, 409);
      }
      return jsonResponse({ok: true});
    }
    if (target === '/api/start') {
      if (startResolver) {
        await new Promise(resolve => { startResolver = resolve; });
        return jsonResponse({ok: true});
      }
      if (rejectNextStart) {
        rejectNextStart = false;
        return jsonResponse({ok: false, error: 'START rejected'}, 409);
      }
      return jsonResponse({ok: true});
    }
    if (target === '/api/jog/hold/start') {
      currentStatus.jog = {...currentStatus.jog, active: true, busy: true};
      return jsonResponse({ok: true});
    }
    if (target === '/api/jog/hold/release') {
      currentStatus.jog = {...currentStatus.jog, active: true, busy: false, direction: null};
      return jsonResponse({ok: true});
    }
    if (target === '/api/archive/part/5') {
      if (!archiveFound) return jsonResponse({error: 'not found'}, 404);
      return jsonResponse({
        part_id: 5,
        meta: {
          category: 'BAD', decision: 'contacts', time: '12:00:00',
          batch_id: 'batch', defects: ['contacts'],
        },
        roles: [{
          role: 'TOP', raw_url: '/raw.jpg',
          raw_overlay_url: '/raw-overlay.jpg', debug_url: '/debug.jpg',
        }],
      });
    }
    if (target === '/api/thresholds' && (options.method || 'GET') === 'POST') {
      const body = JSON.parse(options.body || '{}');
      thresholdsValues = {...thresholdsValues, ...(body.values || {})};
      thresholdsRevision += 1;
      return jsonResponse({
        ok: true,
        thresholds: thresholdsPayload(body.role || 'TOP', thresholdsRevision, thresholdsValues),
      });
    }
    if (target.startsWith('/api/thresholds')) {
      const role = target.includes('role=')
        ? decodeURIComponent(target.split('role=')[1])
        : 'TOP';
      return jsonResponse(thresholdsPayload(role, thresholdsRevision, thresholdsValues));
    }
    return jsonResponse({ok: true});
  };

  for (const scriptPath of scriptPaths) {
    const element = window.document.createElement('script');
    element.textContent = fs.readFileSync(scriptPath, 'utf8');
    window.document.body.appendChild(element);
  }
  const api = window.__TRANSPORTER_UI_TEST_API__;
  assert(api, 'test API is exposed only in test mode');
  api.setupForTest();

  const start = window.document.getElementById('btn-start');
  const stop = window.document.getElementById('btn-stop');
  const exit = window.document.getElementById('btn-exit');
  const diagnostics = [...window.document.querySelectorAll('[data-distributor-command]')];
  const jogButtons = [...window.document.querySelectorAll('.jog-hold-btn')];
  const analyzeSelected = window.document.getElementById('analyze-selected-frame');
  const viewModeToggle = window.document.getElementById('view-mode-toggle');
  const hidden = element => element.classList.contains('is-hidden');

  // 1. No backend state: all controls remain fail-closed.
  assert(start.disabled && stop.disabled && exit.disabled, 'initial main controls disabled');
  assert(diagnostics.every(button => button.disabled), 'initial diagnostics disabled');
  assert(jogButtons.every(button => button.disabled), 'initial JOG disabled');
  assert(analyzeSelected.disabled, 'initial selected-frame analysis disabled');
  assert(viewModeToggle.disabled, 'initial view-mode toggle disabled');

  // 2. IDLE permissions.
  currentStatus = lineStatus('IDLE', {
    diagnostic_allowed: true,
    controls: controls({
      start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true,
    }),
    jog: {
      active: true, can_enter: true, busy: false, hold_steps: 1000000,
      last_action: '-', direction: null, error: null,
    },
  });
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('state-label').textContent === 'ГОТОВА К ПУСКУ', 'IDLE label is unambiguous');
  assert(!hidden(start) && !start.disabled, 'IDLE START enabled');
  assert(hidden(stop), 'IDLE STOP hidden');
  assert(!exit.disabled && exit.textContent === 'ВЫХОД', 'IDLE EXIT enabled');
  assert(diagnostics.every(button => !button.disabled), 'IDLE diagnostics enabled');
  assert(jogButtons.every(button => !button.disabled), 'IDLE JOG enabled');
  assert(viewModeToggle.disabled, 'IDLE view-mode toggle disabled before frame analysis');
  assert(window.document.getElementById('view-mode-toggle').classList.contains('is-faded'), 'IDLE view-mode toggle hidden before frame analysis');
  assert(!window.document.getElementById('stats-body').classList.contains('is-collapsed'), 'IDLE work statistics visible');
  assert(!window.document.getElementById('stats-service').classList.contains('is-collapsed'), 'IDLE service statistics visible');
  assert([...window.document.querySelectorAll('.blade-diagnostic-grid')].every(grid => !grid.classList.contains('is-collapsed')), 'IDLE distributor buttons expanded');

  // 3. Physical distributor diagnostic operation locks every conflicting action.
  currentStatus = lineStatus('IDLE', {
    diagnostic_busy: true,
    process: {phase: 'DISTRIBUTOR_DIAGNOSTIC', label: 'DIST1 OPEN', positions: [], conveyor: {}},
    controls: controls({exit: false}),
    jog: {...currentStatus.jog},
  });
  api.updateLineStatus(currentStatus);
  assert(!hidden(start) && start.disabled, 'diagnostic START visible but disabled');
  assert(exit.disabled, 'diagnostic EXIT disabled');
  assert(diagnostics.every(button => button.disabled), 'diagnostic buttons locked');
  assert(jogButtons.every(button => button.disabled), 'diagnostic JOG locked');

  // 4. RUNNING permissions.
  currentStatus = lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    process: {phase: 'CONVEYOR_MOVING', label: 'moving', positions: [0,1,2,3,4,5,6,7], conveyor: {pos: 10, tgt: 100, mov: 1, wait: 0}},
    frame_analysis: {
      available: true,
      kind: 'CYCLE',
      active: true,
      title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА',
      role: null,
      part_id: 2,
      message: 'Результаты текущего кадра',
      models: [{role: 'TOP', model: 'weights/top.pt', ok: true, elapsed_ms: 12, detections: 2}],
      rules: [{name: 'top_rule', triggered: false, skipped: false, detail: 'Норма'}],
      updated_at: 1,
    },
  });
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('state-label').textContent === 'РАБОТАЕТ', 'RUNNING label is standardized');
  assert(hidden(start), 'RUNNING START hidden');
  assert(!hidden(stop) && !stop.disabled, 'RUNNING STOP enabled');
  assert(!exit.disabled, 'RUNNING graceful EXIT enabled');
  assert(diagnostics.every(button => button.disabled), 'RUNNING diagnostics disabled');
  assert(jogButtons.every(button => button.disabled), 'RUNNING JOG disabled');
  assert(window.document.getElementById('jog-panel').classList.contains('is-collapsed'), 'RUNNING manual conveyor panel hidden');
  assert(!window.document.getElementById('frame-analysis-panel').classList.contains('is-collapsed'), 'RUNNING frame analysis visible');
  assert(!window.document.getElementById('distributor-diagnostics').classList.contains('is-collapsed'), 'RUNNING distributor movement visible');
  assert(!window.document.getElementById('stats-summary').classList.contains('is-collapsed'), 'RUNNING statistics visible');
  assert(!window.document.getElementById('stats-body').classList.contains('is-collapsed'), 'RUNNING work statistics expanded');
  assert(!window.document.getElementById('stats-service').classList.contains('is-collapsed'), 'RUNNING service statistics expanded');
  assert([...window.document.querySelectorAll('.blade-diagnostic-grid')].every(grid => grid.classList.contains('is-collapsed')), 'RUNNING distributor buttons collapsed');
  assert(!viewModeToggle.disabled, 'RUNNING view-mode toggle enabled');
  assert(window.document.getElementById('frame-analysis-message').textContent === 'ВХОД: КОРПУС ПРИНЯТ, ДЕФЕКТОВ НЕТ', 'cycle panel shows operator verdict');
  assert(window.document.getElementById('frame-analysis-context').textContent.includes('КОРПУС #2'), 'cycle panel names the analyzed case');

  // 4b. INPUT stage with a rejected case: verdict and context must be unambiguous
  // (no duplicated «ВХОД · ВХОД» when the role label already starts with the stage).
  currentStatus = lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    process: {phase: 'INPUT_ANALYSIS', label: 'input', positions: [0], conveyor: {}},
    line_parts: [{id: 3, position: 0, category: 'BAD'}],
    frame_analysis: {
      available: true,
      kind: 'CYCLE',
      active: true,
      title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА',
      stage: 'ВХОД',
      role: 'INPUT_LEFT',
      part_id: 3,
      message: 'Результаты текущего кадра',
      models: [],
      rules: [{name: 'presence', triggered: true, skipped: false, detail: 'Сработало'}],
      updated_at: 2,
    },
  });
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('frame-analysis-message').textContent === 'ВХОД: РЕШЕНИЕ — БРАК', 'input rejection verdict shown');
  assert(window.document.getElementById('frame-analysis-context').textContent === 'ВХОД · СЛЕВА · КОРПУС #3', 'context avoids duplicated stage word');

  // 5. STOPPING and DRAINING.
  currentStatus = lineStatus('STOPPING', {
    exit_requested: true,
    controls: controls({exit: true}),
    process: {phase: 'DRAINING', label: 'draining', positions: [], conveyor: {}},
  });
  api.updateLineStatus(currentStatus);
  assert(hidden(start) && hidden(stop), 'STOPPING START/STOP hidden');
  assert(exit.textContent === 'ПРИНУДИТЕЛЬНЫЙ ВЫХОД' && !exit.disabled, 'STOPPING force exit enabled');

  // 6. STOPPED returns pre-start controls.
  currentStatus = lineStatus('STOPPED', {
    diagnostic_allowed: true,
    controls: controls({
      start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true,
    }),
    jog: {
      active: true, can_enter: true, busy: false, hold_steps: 1000000,
      last_action: '-', direction: null, error: null,
    },
  });
  api.updateLineStatus(currentStatus);
  assert(!hidden(start) && !start.disabled, 'STOPPED START enabled');
  assert(diagnostics.every(button => !button.disabled), 'STOPPED diagnostics enabled');
  assert(jogButtons.every(button => !button.disabled), 'STOPPED JOG enabled');
  assert(!window.document.getElementById('jog-panel').classList.contains('is-collapsed'), 'STOPPED manual conveyor panel visible');
  assert(window.document.getElementById('frame-analysis-panel').classList.contains('is-collapsed'), 'frame analysis hidden before analysis');
  assert(!window.document.getElementById('stats-body').classList.contains('is-collapsed'), 'STOPPED work statistics visible');
  assert([...window.document.querySelectorAll('.blade-diagnostic-grid')].every(grid => !grid.classList.contains('is-collapsed')), 'STOPPED distributor buttons expanded');

  // 7. FAULT has exactly FORCE EXIT and visible reason.
  currentStatus = lineStatus('FAULT', {
    fault_reason: 'camera disconnected',
    controls: controls({exit: true}),
    process: {phase: 'FAULT', label: 'camera disconnected', positions: [], conveyor: {}},
  });
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('state-label').textContent === 'АВАРИЯ', 'FAULT label is standardized');
  assert(hidden(start) && hidden(stop), 'FAULT START/STOP hidden');
  assert(exit.textContent === 'ПРИНУДИТЕЛЬНЫЙ ВЫХОД' && !exit.disabled, 'FAULT FORCE EXIT enabled');
  assert(diagnostics.every(button => button.disabled), 'FAULT diagnostics disabled');
  assert(jogButtons.every(button => button.disabled), 'FAULT JOG disabled');
  assert(window.document.getElementById('camera-overlay').textContent.includes('camera disconnected'), 'FAULT reason visible');

  // 8. Part-path rendering and process highlights.
  const line = lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    line_parts: [
      {id: 1, position: 0, category: 'UNKNOWN'},
      {id: 2, position: 4, category: 'BAD'},
      {id: 3, position: 7, category: 'CLEANUP'},
    ],
    process: {
      phase: 'SPIDER_ANALYSIS', label: 'models + rules', part_id: 2,
      positions: [4], conveyor: {pos: 100, tgt: 100, mov: 0, wait: 0},
    },
  });
  const lineCellsEl = window.document.getElementById('line-cells');
  const cells = [...lineCellsEl.querySelectorAll('.line-cell')];
  // jsdom не делает раскладку: подставляем геометрию ячеек, чтобы
  // маркеры деталей (.line-token) получили реальные координаты.
  const stubRect = (el, rect) => {
    el.getBoundingClientRect = () => ({x: rect.left, y: rect.top, toJSON() { return {}; }, ...rect});
  };
  stubRect(lineCellsEl, {left: 0, top: 0, width: 832, height: 34, right: 832, bottom: 34});
  cells.forEach((cell, index) => {
    stubRect(cell, {
      left: index * 104, top: 0, width: 100, height: 30,
      right: index * 104 + 100, bottom: 30,
    });
  });
  api.updateLineStatus(line);
  const tokens = [...lineCellsEl.querySelectorAll('.line-token')];
  const tokenById = id => tokens.find(token => token.dataset.partId === String(id));
  assert(tokens.length === 3, 'по одному маркеру на каждую деталь линии');
  assert(cells.every(cell => cell.textContent === ''), 'ячейки не содержат своего текста');
  assert(tokenById(1).textContent === '#1' && tokenById(1).style.left === '0px', 'INPUT part ID rendered at first cell');
  assert(tokenById(2).textContent === '#2' && tokenById(2).style.left === '416px' && tokenById(2).classList.contains('cell-bad'), 'SPIDER token carries its category');
  assert(tokenById(3).textContent === '#3' && tokenById(3).classList.contains('cell-cleanup'), 'ROUTE cleanup rendered');
  assert(cells[4].classList.contains('process-camera'), 'SPIDER cell highlighted');

  // Шаг линии: маркеры сдвигаются ровно на одну ячейку вправо, как лента.
  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    line_parts: [
      {id: 1, position: 1, category: 'UNKNOWN'},
      {id: 2, position: 5, category: 'BAD'},
    ],
    process: {
      phase: 'CONVEYOR_MOVING', label: 'belt moves', part_id: null,
      positions: [], conveyor: {speed: 20000, pos: 50, tgt: 100, mov: 1, wait: 0, lasterr: 0},
    },
  }));
  assert(tokenById(1).style.left === '104px', 'маркер детали сдвинулся на одну ячейку вместе с лентой');
  assert(tokenById(2).style.left === '520px', 'маркер #2 повторяет движение конвейера');
  assert(lineCellsEl.querySelector('.conveyor-belt').classList.contains('moving'), 'лента подсвечена во время проезда');
  await sleep(620);
  assert(!lineCellsEl.querySelector('.line-token[data-part-id="3"]'), 'сошедшая с линии деталь удалена после проезда');
  api.updateRecentParts([{id: 2, category: 'BAD', decision: 'contacts'}]);
  assert(window.document.getElementById('defects-title').textContent === 'ОСНОВНЫЕ ДЕФЕКТЫ', 'top defects shown while running');
  assert(!window.document.getElementById('defects-section').classList.contains('is-hidden'), 'working defects section visible');

  // 8.1 Вся панель распределителя заливается цветом маршрута детали.
  const distPanel = window.document.getElementById('distributor-diagnostics');
  const distRoute = window.document.getElementById('dist-route');
  const routeClasses = ['route-good', 'route-bad', 'route-cleanup'];
  assert(!window.document.getElementById('scada-actuator'), 'SCADA-блок «МЕХАНИКА ЛИНИИ» удалён из DOM');

  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    line_parts: [{id: 9, position: 6, category: 'BAD'}],
    process: {
      phase: 'ROUTE_PREPARE', label: 'route', part_id: 9,
      positions: [7], conveyor: {},
    },
  }));
  assert(distPanel.classList.contains('route-bad'), 'панель красная, когда деталь уходит в брак');
  assert(distRoute.textContent === '→ БРАК', 'подпись маршрута показывает канал БРАК');

  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    line_parts: [{id: 10, position: 7, category: 'GOOD'}],
    process: {
      phase: 'SETTLE', label: 'settle', part_id: null,
      positions: [], conveyor: {},
    },
  }));
  assert(distPanel.classList.contains('route-good'), 'панель зелёная, когда деталь уходит в годное');
  assert(!distPanel.classList.contains('route-bad'), 'красная заливка снята');

  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    line_parts: [{id: 11, position: 7, category: 'CLEANUP'}],
    process: {
      phase: 'PART_DROP', label: 'drop', part_id: 11,
      positions: [7], conveyor: {},
    },
  }));
  assert(distPanel.classList.contains('route-cleanup'), 'панель жёлтая, когда деталь уходит на очистку');

  api.updateLineStatus(lineStatus('IDLE', {
    controls: controls({start: true, exit: true}),
  }));
  assert(
    !routeClasses.some(cls => distPanel.classList.contains(cls)),
    'без детали на сортировке панель не залита',
  );
  assert(distPanel.classList.contains('production-ready'), 'припаркованный распределитель подсвечен зелёным');
  assert(distRoute.textContent === 'ПРОИЗВОДСТВО ГОТОВО', 'подпись готовности к пуску видна');

  // Команда ПРОХОД (заслонка закрывается к 0) зажигает зелёное сразу,
  // не дожидаясь фактического прихода ползунка в исходное положение —
  // как СБРОС зажигает красное в момент команды.
  api.updateLineStatus(lineStatus('IDLE', {
    dist1_position: 340, dist1_state: 'CLOSING',
    controls: controls({start: true, exit: true}),
  }));
  assert(distPanel.classList.contains('production-ready'), 'закрытие к HOME подсвечено зелёным сразу');
  assert(!distPanel.classList.contains('route-bad'), 'красный не держится во время закрытия');
  assert(distRoute.textContent === 'ПРОИЗВОДСТВО ГОТОВО', 'подпись готовности видна и при закрытии');
  api.updateLineStatus(lineStatus('IDLE', {
    dist1_position: 0, dist1_state: 'IDLE',
    controls: controls({start: true, exit: true}),
  }));
  assert(distPanel.classList.contains('production-ready'), 'после прихода в 0 зелёный остаётся');

  // Открытая вручную заслонка сброса заливает панель по каналу DIST2.
  api.updateLineStatus(lineStatus('IDLE', {
    dist1_position: 340, dist1_state: 'OPEN', dist2_target: 'CLEANUP',
    controls: controls({start: true, exit: true}),
  }));
  assert(distPanel.classList.contains('route-cleanup'), 'ручной сброс показывает канал очистки');
  api.updateLineStatus(lineStatus('IDLE', {
    dist1_position: 340, dist1_state: 'OPEN', dist2_target: 'BAD',
    controls: controls({start: true, exit: true}),
  }));
  assert(distPanel.classList.contains('route-bad'), 'ручной сброс показывает канал брака');
  api.updateLineStatus(lineStatus('IDLE', {
    controls: controls({start: true, exit: true}),
  }));
  assert(
    !routeClasses.some(cls => distPanel.classList.contains(cls)),
    'закрытая заслонка гасит заливку панели',
  );

  // 9. Actual distributor coordinates map to blade marker positions.
  api.updateLineStatus(lineStatus('IDLE', {
    dist1_position: -2000000,
    dist2_position: -15,
    controls: controls({start: true, exit: true}),
  }));
  assert(window.document.getElementById('dist1-pos').textContent === '0', 'negative DIST1 homing sentinel clamped to zero');
  assert(window.document.getElementById('dist2-pos').textContent === '0', 'negative DIST2 homing coordinate clamped to zero');
  assert(window.document.getElementById('dist1-blade').style.left === '0%', 'negative DIST1 marker clamped to left endpoint');
  assert(window.document.getElementById('dist2-target').textContent === 'БРАК', 'actual DIST2 channel remains visible');

  currentStatus = lineStatus('IDLE', {
    dist1_position: 170, dist1_max: 340,
    dist2_position: 340, dist2_max: 340,
    diagnostic_allowed: true,
    controls: controls({start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true}),
    jog: {...currentStatus.jog, active: true, can_enter: true},
  });
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('dist1-blade').style.left === '50%', 'DIST1 intermediate position centered');
  assert(window.document.getElementById('dist2-blade').style.left === '100%', 'DIST2 marker reaches exact right endpoint');

  // 10. All four diagnostics call exact endpoints; pending state locks UI.
  for (const button of diagnostics) {
    calls.length = 0;
    button.disabled = false;
    button.click();
    await sleep(5);
    assert(calls.some(call => call.url.endsWith(button.dataset.distributorCommand)), `diagnostic ${button.dataset.distributorCommand}`);
  }
  diagnosticFailure = true;
  diagnostics[0].disabled = false;
  diagnostics[0].click();
  await sleep(5);
  assert(window.document.getElementById('control-error').textContent.includes('diagnostic rejected'), 'diagnostic error visible');

  // 11. Double START click is serialized locally.
  currentStatus = lineStatus('IDLE', {
    diagnostic_allowed: true,
    controls: controls({start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true}),
    jog: {...currentStatus.jog, active: true, can_enter: true},
  });
  api.updateLineStatus(currentStatus);
  calls.length = 0;
  startResolver = true;
  start.click(); start.click();
  await sleep(5);
  assert(calls.filter(call => call.url === '/api/start').length === 1, 'double START suppressed');
  assert(window.document.getElementById('camera-overlay').textContent.includes('ПОДГОТОВКА К ПУСКУ'), 'START never flashes stopped overlay');
  startResolver(); startResolver = null;
  await sleep(5);
  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
  }));
  assert(!api.state.startPending, 'START pending clears on RUNNING state');
  currentStatus = lineStatus('IDLE', {
    diagnostic_allowed: true,
    controls: controls({start: true, exit: true}),
  });
  api.updateLineStatus(currentStatus);
  rejectNextStart = true;
  start.click();
  await sleep(5);
  assert(window.document.getElementById('control-error').textContent.includes('START rejected'), 'START rejection visible');

  calls.length = 0;
  currentStatus = lineStatus('RUNNING', {controls: controls({stop: true, exit: true})});
  api.updateLineStatus(currentStatus);
  stop.click();
  await sleep(5);
  assert(calls.filter(call => call.url === '/api/stop').length === 1, 'STOP click sends once');

  calls.length = 0;
  currentStatus = lineStatus('STOPPED', {controls: controls({start: true, exit: true})});
  api.updateLineStatus(currentStatus);
  exit.click();
  await sleep(5);
  assert(calls.filter(call => call.url === '/api/exit').length === 1, 'EXIT click sends once');

  // 12. RAW/RULES is unavailable before a selected-frame analysis.
  api.state.jogActive = true;
  api.updateViewModeControls();
  calls.length = 0;
  window.document.getElementById('mode-badge').click();
  await api.setViewMode('RAW');
  assert(!calls.some(call => call.url.startsWith('/api/mode/')), 'mode controls blocked before analysis');
  assert(viewModeToggle.disabled, 'view-mode toggle disabled before analysis');
  assert(window.document.getElementById('view-mode-toggle').classList.contains('is-faded'), 'view-mode toggle hidden before analysis');

  // 13. Camera list, click, numeric and rapid selection keep latest role.
  calls.length = 0;
  await api.fetchCameras();
  assert(window.document.querySelectorAll('.preview-cam').length === 7, 'seven camera previews');
  activeCameraDelay = 5;
  api.selectCamera('TOP');
  api.selectCamera('SPIDER_IN');
  await sleep(30);
  const activeCalls = calls.filter(call => call.url.startsWith('/api/active_camera/'));
  assert(activeCalls.at(-1).url.endsWith('/SPIDER_IN'), 'latest rapid camera selection wins');
  assert(window.document.getElementById('camera-label').textContent === 'ВНУТРЕННИЙ ВИД', 'camera label follows selection');
  activeCameraDelay = 0;

  // 14. Selected LIVE camera uses max-FPS status and on-demand model snapshot.
  currentStatus.controls = controls({
    start: true, exit: true, jog_hold: true,
    selected_model_analysis: true,
    distributor_diagnostic: true,
    camera_diagnostic: true,
    vision_rule_diagnostic: true,
  });
  currentStatus.jog = {...currentStatus.jog, active: true, live_fps: 30.0};
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('mode-badge').textContent === 'ПОТОК · 30,0 КАДР/С', 'live badge includes the frame rate');
  assert(api.state.mainCamMode === 'live-pull', 'selected live camera uses completed HTTP pulls');
  assert(api.getMainBufferSource().includes('mode=RULES'), 'live pull keeps selected RULES view');
  // Три кадра прогонов доступны: фронт включает переключение по клику.
  api.state.runFramesAvailable = 3;
  api.updateRunCycleAvailability();
  calls.length = 0;
  analyzeSelected.click();
  await sleep(45);
  assert(calls.some(call => call.url.endsWith('/api/diagnostics/selected/SPIDER_IN')), 'selected model endpoint');
  assert(analyzeSelected.textContent === 'ВЕРНУТЬ ПОТОК', 'analysis button becomes return-live');
  assert(window.document.getElementById('mode-badge').textContent === 'АНАЛИЗ · ПРОГОН 2/3', 'live badge is replaced during analysis and names the server-selected run');
  assert(window.document.getElementById('camera-label').textContent === 'ВНУТРЕННИЙ ВИД · АНАЛИЗ', 'camera label names the frozen analysis frame');
  assert(window.document.getElementById('camera-overlay').classList.contains('is-hidden'), 'idle/stop overlay does not cover frozen analysis frame');
  assert(window.document.getElementById('main-camera').src.includes('mode=RULES'), 'selected rule overlay shown');
  assert(!window.document.getElementById('frame-analysis-panel').classList.contains('is-collapsed'), 'selected frame analysis replaces right panel');
  assert(window.document.getElementById('stats-summary').classList.contains('is-collapsed'), 'statistics hidden while selected frame is frozen');
  assert(window.document.getElementById('distributor-diagnostics').classList.contains('is-collapsed'), 'distributor hidden while selected frame is frozen');
  assert(window.document.getElementById('jog-panel').classList.contains('is-collapsed'), 'manual controls hidden while selected frame is frozen');
  assert(window.document.querySelectorAll('#frame-analysis-models .frame-analysis-item').length === 2, 'selected models listed');
  assert(window.document.querySelectorAll('#frame-analysis-rules .frame-analysis-item').length === 2, 'selected rules listed');
  // Во время анализа кадра блок «Пороги правил» не показывается.
  assert(window.document.getElementById('thresholds-panel').classList.contains('is-hidden'), 'во время анализа кадра пороги правил скрыты');
  // Блок «какие модели сработали» свёрнут по умолчанию, разворачивается по клику.
  const modelsList = window.document.getElementById('frame-analysis-models');
  const modelsToggle = window.document.getElementById('frame-analysis-models-toggle');
  assert(modelsList.classList.contains('frame-analysis-list-collapsed'), 'модели свёрнуты по умолчанию');
  assert(modelsToggle.getAttribute('aria-expanded') === 'false', 'toggle свёрнут по умолчанию');
  modelsToggle.click();
  assert(!modelsList.classList.contains('frame-analysis-list-collapsed'), 'модели разворачиваются по клику');
  assert(modelsToggle.getAttribute('aria-expanded') === 'true', 'toggle развёрнут');
  // Правила показывают компактно: название, рядом порог, под ним три
  // замера по трём прогонам голосования 2 из 3.
  const ruleItems = [...window.document.querySelectorAll('#frame-analysis-rules .frame-analysis-item')];
  const measurementBlocks = [...window.document.querySelectorAll(
    '#frame-analysis-rules .fa-measurements',
  )];
  assert(measurementBlocks.length === 2, 'у каждого правила есть блок замеров');
  const rows = [...window.document.querySelectorAll(
    '#frame-analysis-rules .fa-measurement-row',
  )];
  assert(rows.length >= 2, 'под правилом собраны пороги с тремя замерами');
  assert(
    rows.some(row => row.textContent.includes('порог: 0.8 px')),
    'рядом с правилом показывается порог',
  );
  assert(
    rows.some(row => row.textContent.includes('0.5 px') && row.textContent.includes('0.79 px')),
    'под порогом три замера по прогонам',
  );
  // Замер выбранного для картинки прогона (picture_run=2) помечается.
  assert(
    rows.some(row => row.querySelector('.fa-measurement-value.is-picture-run')),
    'замер выбранного для картинки прогона помечен',
  );
  assert(
    rows.some(row => row.querySelector('.fa-measurement-value.is-bad')),
    'замер за порогом подсвечен',
  );
  // Пропущенная метрика в одном из прогонов не сдвигает замеры: слоты
  // фиксированы номерами прогонов, отсутствующий замер — прочерк.
  const synthetic = {
    name: 'synth_rule', triggered: false, skipped: false, status_label: 'НОРМА',
    run_cards: [
      [{role: 'TOP', metrics: [{label: 'Метрика', value: '1 px', limit: '3 px', ok: true}]}],
      [],
      [{role: 'TOP', metrics: [{label: 'Метрика', value: '3 px', limit: '3 px', ok: true}]}],
    ],
  };
  window.eval(`renderFrameAnalysisRules(${JSON.stringify([synthetic])}, 3);`);
  const synthRow = window.document.querySelector('#frame-analysis-rules .fa-measurement-row');
  const synthChips = [...synthRow.querySelectorAll('.fa-measurement-value')];
  assert(synthChips.length === 3, 'три слота замеров даже при пропуске в прогоне');
  assert(synthChips[0].textContent === '1 px', 'замер первого прогона на своём месте');
  assert(synthChips[1].textContent === '—', 'пропущенный замер — прочерк');
  assert(synthChips[2].classList.contains('is-picture-run'), 'рамка выбранного прогона не сдвинулась');

  // Правило с непостроенной областью (fail-closed): показывается полоса
  // статусов прогонов «ОБЛАСТЬ НЕ ПОСТРОЕНА», даже без замеров.
  const regionRule = {
    name: 'long_omission', triggered: true, skipped: false,
    status_label: 'СРАБОТАЛО · 2/3',
    run_status: [
      [{role: 'SPIDER_LEFT', status: 'ОБЛАСТЬ НЕ ПОСТРОЕНА', reason: 'no_detections'}],
      [{role: 'SPIDER_LEFT', status: 'В НОРМЕ', reason: null}],
      [{role: 'SPIDER_LEFT', status: 'ОБЛАСТЬ НЕ ПОСТРОЕНА', reason: 'no_detections'}],
    ],
  };
  window.eval(`renderFrameAnalysisRules(${JSON.stringify([regionRule])}, 1);`);
  const statusChips = [...window.document.querySelectorAll(
    '#frame-analysis-rules .fa-run-status-chip',
  )];
  assert(statusChips.length === 3, 'полоса статусов по прогонам');
  assert(
    statusChips[0].textContent.includes('ОБЛАСТЬ НЕ ПОСТРОЕНА') &&
      statusChips[0].textContent.includes('no_detections'),
    'статус «область не построена» с причиной',
  );
  assert(statusChips[0].classList.contains('is-bad'), 'плохой прогон подсвечен');
  assert(statusChips[1].classList.contains('is-ok'), 'нормальный прогон подсвечен');
  assert(statusChips[0].classList.contains('is-picture-run'), 'выбранный прогон помечен');

  // Возвращаем прежний отчёт (меняем updated_at, чтобы ключ рендера сменился).
  currentStatus.frame_analysis.updated_at += 1;
  api.updateLineStatus(currentStatus);
  assert(!viewModeToggle.disabled, 'view-mode toggle enabled during selected analysis');

  // Почему выбран этот прогон — видно под вердиктом анализа.
  const pictureLine = window.document.getElementById('frame-analysis-picture');
  assert(!pictureLine.classList.contains('is-hidden'), 'строка «почему этот прогон» видна');
  assert(
    pictureLine.textContent.includes('ПРОГОН 2') &&
      pictureLine.textContent.includes('ближе всего к порогу'),
    'строка объясняет выбор прогона',
  );

  // Клик по главному кадру переключает кадры трёх прогонов анализа.
  assert(api.state.viewRun === 2, 'новый анализ показывает выбранный сервером прогон');
  assert(
    window.document.getElementById('main-camera').src.includes('run=2'),
    'главный кадр запрашивает кадр прогона 2',
  );
  api.cycleMainCameraRun();
  assert(api.state.viewRun === 3, 'цикл переключает на следующий прогон');
  assert(
    window.document.getElementById('main-camera').src.includes('run=3'),
    'главный кадр запрашивает кадр прогона 3',
  );
  assert(
    window.document.getElementById('mode-badge').textContent === 'АНАЛИЗ · ПРОГОН 3/3',
    'бейдж показывает выбранный прогон',
  );
  assert(
    [...window.document.querySelectorAll(
      '#frame-analysis-rules .fa-measurement-value.is-picture-run',
    )].some(chip => chip.textContent === '0.9 px'),
    'рамка замера следует за выбранным прогоном',
  );
  // Настоящий клик по контейнеру камеры циклит дальше (1/3).
  window.document.querySelector('.camera-container')
    .dispatchEvent(new window.MouseEvent('click', {bubbles: true}));
  assert(api.state.viewRun === 1, 'клик по кадру переключает прогон');
  assert(
    window.document.getElementById('mode-badge').textContent === 'АНАЛИЗ · ПРОГОН 1/3',
    'бейдж после клика по кадру',
  );
  // Горячая клавиша N (физический код KeyN) циклит прогоны так же, как клик.
  window.document.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: 'n', code: 'KeyN', bubbles: true,
  }));
  assert(api.state.viewRun === 2, 'клавиша N переключает на следующий прогон');
  assert(
    window.document.getElementById('mode-badge').textContent === 'АНАЛИЗ · ПРОГОН 2/3',
    'бейдж после клавиши N',
  );
  // Без кадров прогонов (например, живой поток) ни клик, ни N не меняют.
  api.state.runFramesAvailable = 0;
  api.updateRunCycleAvailability();
  const runBeforeNoFrames = api.state.viewRun;
  window.document.querySelector('.camera-container')
    .dispatchEvent(new window.MouseEvent('click', {bubbles: true}));
  assert(api.state.viewRun === runBeforeNoFrames,
    'клик без кадров прогонов не переключает');
  window.document.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: 'n', code: 'KeyN', bubbles: true,
  }));
  assert(api.state.viewRun === runBeforeNoFrames,
    'клавиша N без кадров прогонов не переключает');
  calls.length = 0;
  await api.setViewMode('RAW');
  assert(calls.some(call => call.url === '/api/mode/RAW'), 'RAW enabled during selected analysis');
  assert(window.document.getElementById('main-camera').src.includes('mode=RAW'), 'selected RAW overlay shown');
  calls.length = 0;
  viewModeToggle.click();
  await sleep(10);
  assert(calls.some(call => call.url === '/api/mode/RULES'), 'single toggle click switches RAW -> RULES');
  assert(viewModeToggle.textContent === 'ВИД: ПРАВИЛА', 'toggle label follows current mode');
  assert(window.document.getElementById('defects-section').classList.contains('is-hidden'), 'old statistics analysis summary is not used');
  const frozenCamera = window.document.getElementById('camera-label').textContent;
  api.selectCamera('TOP');
  assert(window.document.getElementById('camera-label').textContent === frozenCamera, 'camera switch blocked during frozen analysis');
  assert(start.disabled && diagnostics.every(button => button.disabled), 'selected analysis blocks START and distributor');
  assert(jogButtons.every(button => button.disabled), 'selected analysis blocks JOG');
  analyzeSelected.click();
  await sleep(45);
  assert(calls.some(call => call.url === '/api/diagnostics/selected/release'), 'return LIVE endpoint');
  assert(analyzeSelected.textContent === 'АНАЛИЗ КАДРА', 'analysis button restored');
  assert(window.document.getElementById('mode-badge').textContent.includes('ПОТОК'), 'live badge restored');
  assert(window.document.getElementById('camera-label').textContent === 'ВНУТРЕННИЙ ВИД', 'camera label restored after returning to live');
  assert(window.document.getElementById('frame-analysis-panel').classList.contains('is-collapsed'), 'selected analysis is cleared after returning to live');
  assert(!window.document.getElementById('stats-summary').classList.contains('is-collapsed'), 'statistics restored after selected analysis');
  assert(!window.document.getElementById('distributor-diagnostics').classList.contains('is-collapsed'), 'distributor restored after selected analysis');
  assert(!window.document.getElementById('jog-panel').classList.contains('is-collapsed'), 'manual controls restored after selected analysis');
  assert(window.document.getElementById('view-mode-toggle').classList.contains('is-faded'), 'view-mode toggle hidden after selected analysis');

  api.state.jogActive = false;
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: '1', bubbles: true}));
  assert(window.document.getElementById('camera-label').textContent === 'ВХОД · СЛЕВА', 'numeric camera hotkey');
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  assert(window.document.getElementById('camera-label').textContent === 'ВХОД · СПРАВА', 'camera arrow navigation');
  const input = window.document.createElement('input');
  window.document.body.appendChild(input);
  const beforeInputKey = window.document.getElementById('camera-label').textContent;
  input.dispatchEvent(new window.KeyboardEvent('keydown', {key: '3', bubbles: true}));
  assert(window.document.getElementById('camera-label').textContent === beforeInputKey, 'hotkeys ignored in input');
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'F11', bubbles: true}));

  // 14. JOG pointerdown + every release path produces hold/release requests.
  api.state.jogActive = true;
  api.state.backendControls = controls({start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true});
  const left = jogButtons[0];
  async function jogScenario(releaseEvent) {
    currentStatus = lineStatus('IDLE', {
      diagnostic_allowed: true,
      controls: controls({start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true}),
      jog: {active: true, can_enter: true, busy: false, hold_steps: 1000000, last_action: '-', direction: null, error: null},
    });
    api.clearJogHoldLocalState();
    api.updateLineStatus(currentStatus);
    calls.length = 0;
    left.dispatchEvent(new window.Event('pointerdown', {bubbles: true}));
    await sleep(10);
    if (releaseEvent === 'blur') {
      window.dispatchEvent(new window.Event('blur'));
    } else if (releaseEvent === 'pagehide' || releaseEvent === 'beforeunload') {
      window.dispatchEvent(new window.Event(releaseEvent));
    } else if (releaseEvent === 'visibilitychange') {
      Object.defineProperty(window.document, 'hidden', {value: true, configurable: true});
      window.document.dispatchEvent(new window.Event('visibilitychange'));
      Object.defineProperty(window.document, 'hidden', {value: false, configurable: true});
    } else {
      const event = new window.Event(releaseEvent, {bubbles: true});
      if (releaseEvent === 'pointerleave') Object.defineProperty(event, 'buttons', {value: 0});
      left.dispatchEvent(event);
    }
    await sleep(10);
    assert(calls.some(call => call.url === '/api/jog/hold/start'), `JOG start for ${releaseEvent}`);
    assert(calls.some(call => call.url === '/api/jog/hold/heartbeat'), `JOG heartbeat for ${releaseEvent}`);
    assert(calls.some(call => call.url === '/api/jog/hold/release'), `JOG release for ${releaseEvent}`);
    api.clearJogHoldLocalState();
  }
  for (const event of [
    'pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave',
    'blur', 'visibilitychange', 'pagehide', 'beforeunload',
  ]) {
    await jogScenario(event);
  }

  // 15. Keyboard hold/release.
  currentStatus = lineStatus('IDLE', {
    diagnostic_allowed: true,
    controls: controls({start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true}),
    jog: {active: true, can_enter: true, busy: false, hold_steps: 1000000, last_action: '-', direction: null, error: null},
  });
  api.clearJogHoldLocalState();
  api.updateLineStatus(currentStatus);
  calls.length = 0;
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  await sleep(10);
  window.dispatchEvent(new window.KeyboardEvent('keyup', {key: 'ArrowRight', bubbles: true}));
  await sleep(10);
  assert(calls.some(call => call.url === '/api/jog/hold/start'), 'keyboard JOG start');
  assert(calls.some(call => call.url === '/api/jog/hold/release'), 'keyboard JOG release');

  // 16. History cards, gallery, modes, fullscreen, close and missing archive.
  api.updateRecentParts([
    {id: 4, category: 'GOOD', decision: 'none'},
    {id: 5, category: 'BAD', decision: 'contacts'},
  ]);
  assert(window.document.querySelectorAll('.history-card').length === 2, 'history cards render');
  await api.openGallery(5);
  const modal = window.document.getElementById('gallery-modal');
  assert(!modal.classList.contains('is-hidden'), 'gallery opens');
  assert(window.document.getElementById('gallery-category').textContent === 'КАТЕГОРИЯ: БРАК', 'gallery metadata');
  window.document.getElementById('gallery-mode-raw').click();
  assert(window.document.querySelector('.gallery-card img').src.includes('raw-overlay.jpg'), 'gallery raw mode');
  const firstGalleryImg = window.document.querySelector('.gallery-card img');
  window._galleryImageError(firstGalleryImg);
  assert(firstGalleryImg.closest('.gallery-card-img-wrap').classList.contains('image-error'), 'gallery missing image placeholder');
  window._galleryImageLoaded(firstGalleryImg);
  assert(!firstGalleryImg.closest('.gallery-card-img-wrap').classList.contains('image-error'), 'gallery image placeholder clears after load');
  window._galleryFullscreen('/debug.jpg');
  assert(window.document.querySelector('.gallery-fullscreen'), 'fullscreen opens');
  window.document.querySelector('.gallery-fullscreen').click();
  assert(!window.document.querySelector('.gallery-fullscreen'), 'fullscreen closes');
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  assert(modal.classList.contains('is-hidden'), 'ESC closes gallery');
  await api.openGallery(5);
  window.document.getElementById('gallery-close').click();
  assert(modal.classList.contains('is-hidden'), 'close button closes gallery');
  await api.openGallery(5);
  window.document.querySelector('.gallery-backdrop').click();
  assert(modal.classList.contains('is-hidden'), 'backdrop closes gallery');
  archiveFound = false;
  await api.openGallery(5);
  assert(window.document.getElementById('gallery-grid').textContent.includes('Корпус не найден'), 'missing archive shown');
  api.closeGallery();

  // 17. API command error is visible and a later explicit action may clear it.
  api.showControlError('START rejected');
  assert(!window.document.getElementById('control-error').classList.contains('is-hidden'), 'control error visible');
  assert(window.document.getElementById('control-error').textContent.includes('START rejected'), 'control error text');
  api.clearControlError();
  assert(window.document.getElementById('control-error').classList.contains('is-hidden'), 'control error clears explicitly');
  api.updateLineStatus({});
  assert(start.disabled && exit.disabled, 'malformed status keeps main controls disabled');
  assert(diagnostics.every(button => button.disabled), 'malformed status keeps diagnostics disabled');
  assert(jogButtons.every(button => button.disabled), 'malformed status keeps JOG disabled');

  // 18. FAULT and OFFLINE lockout + recovery.
  api.updateLineStatus(lineStatus('FAULT', {
    fault_reason: 'model failed', controls: controls({exit: true}),
    process: {phase: 'FAULT', label: 'model failed', positions: [], conveyor: {}},
  }));
  assert(exit.textContent === 'ПРИНУДИТЕЛЬНЫЙ ВЫХОД' && !exit.disabled, 'FAULT only force exit');
  api.markUiOffline();
  assert(window.document.getElementById('state-label').textContent === 'НЕТ СВЯЗИ', 'OFFLINE shown');
  assert(start.disabled && stop.disabled && exit.disabled, 'OFFLINE main controls locked');
  assert(diagnostics.every(button => button.disabled), 'OFFLINE diagnostics locked');
  assert(jogButtons.every(button => button.disabled), 'OFFLINE JOG locked');
  api.state.offline = false;
  window.document.getElementById('main').classList.remove('ui-offline');
  api.updateLineStatus(lineStatus('STOPPED', {
    diagnostic_allowed: true,
    controls: controls({start: true, exit: true, jog_hold: true, selected_model_analysis: true, distributor_diagnostic: true, camera_diagnostic: true, vision_rule_diagnostic: true}),
    jog: {active: true, can_enter: true, busy: false, hold_steps: 1000000, last_action: '-', direction: null, error: null},
  }));
  assert(!start.disabled && diagnostics.every(button => !button.disabled), 'recovery restores backend permissions');

  // 20. PAUSE inside the cycle exposes unrestricted manual JOG before neural networks run.
  const btnPause = window.document.getElementById('btn-pause');
  const btnResume = window.document.getElementById('btn-resume');
  const jogPanel = window.document.getElementById('jog-panel');

  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true, pause: true}),
  }));
  assert(!hidden(btnPause) && !btnPause.disabled, 'RUNNING exposes PAUSE');
  assert(hidden(btnResume), 'RUNNING hides RESUME');
  assert(jogPanel.classList.contains('is-collapsed'), 'RUNNING hides JOG panel');

  calls.length = 0;
  btnPause.click();
  await sleep(5);
  assert(calls.some(call => call.url === '/api/pause'), 'PAUSE button calls /api/pause');

  currentStatus = lineStatus('PAUSED', {
    controls: controls({stop: true, exit: true, resume: true, jog_hold: true}),
    jog: {active: true, can_enter: true, busy: false, hold_steps: 1000000, last_action: '-', direction: null, error: null},
  });
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('state-label').textContent === 'ПАУЗА · КОРРЕКЦИЯ ЛЕНТЫ', 'PAUSED label is explicit');
  assert(hidden(btnPause) && !hidden(btnResume) && !btnResume.disabled, 'PAUSED swaps PAUSE for RESUME');
  assert(!hidden(stop) && !stop.disabled, 'PAUSED still allows normal STOP');
  assert(!jogPanel.classList.contains('is-collapsed'), 'PAUSED reveals JOG panel');
  assert(jogButtons.every(button => !button.disabled), 'PAUSED enables unrestricted JOG buttons');

  calls.length = 0;
  btnResume.click();
  await sleep(5);
  assert(calls.some(call => call.url === '/api/resume'), 'RESUME button calls /api/resume');

  // 21. Splash startup error has an explicit close action.
  calls.length = 0;
  window.document.getElementById('splash-exit').disabled = false;
  window.document.getElementById('splash-exit').click();
  await sleep(5);
  assert(calls.some(call => call.url === '/api/exit'), 'splash close calls EXIT');

  // 22. Mode badge survives the per-tick line_status -> mode update order.
  // fetchStatus calls updateLineStatus (which paints the badge) and then
  // updateMode; applyModeUI must not re-fade a badge applyLiveBadge just
  // made visible.
  const badge = window.document.getElementById('mode-badge');
  api.updateLineStatus(lineStatus('RUNNING', {
    live: {running: true, streaming: true, static: false, fps: 25.0, error: null},
    controls: controls({stop: true, exit: true, pause: true}),
  }));
  api.updateMode('RULES');
  assert(!badge.classList.contains('is-faded'), 'live badge stays visible after mode tick');
  assert(badge.classList.contains('mode-live'), 'live badge keeps mode-live class');
  api.updateLineStatus(lineStatus('STOPPING', {
    live: {running: true, streaming: false, static: true, fps: 0, error: null},
    controls: controls({exit: true}),
  }));
  api.updateMode('RULES');
  assert(!badge.classList.contains('is-faded')
    && badge.classList.contains('mode-static')
    && badge.textContent === 'СТОП-КАДР · ПРАВИЛА',
    'static badge stays visible after mode tick');
  api.updateMode('RAW');
  assert(badge.textContent === 'СТОП-КАДР · RAW',
    'static badge follows the RAW view mode');
  api.updateMode('RULES');
  api.updateLineStatus(lineStatus('IDLE', {
    live: {running: false, streaming: false, static: false, fps: 0, error: null},
    controls: controls({start: true, exit: true}),
  }));
  api.updateMode('RULES');
  assert(badge.classList.contains('is-faded'), 'badge hidden again at rest');

  // 23. Пороги правил: блоки-карточки категорий, значения — числовым
  // полем, у каждой карточки собственный ползунок прокрутки строк
  // (появляется только при переполнении), навигация между карточками,
  // СОХРАНИТЬ реально сохраняет. Заголовки правил «разъезжаются» по
  // наведению — вкладка раскрывается до полного названия.
  // JOG-режим (jog.active) в IDLE включается автоматически и не должен
  // блокировать редактирование — блокирует только реальное движение.
  // jsdom не знает matchMedia: подменяем до первого рендера панели,
  // иначе раскрытие заголовков по наведению не подключится.
  window.matchMedia = () => ({
    matches: true, media: '', onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {},
    removeEventListener() {}, dispatchEvent() { return false; },
  });
  currentStatus = lineStatus('IDLE', {
    diagnostic_allowed: true,
    jog: {
      active: true, can_enter: true, busy: false, hold_steps: 1000000,
      last_action: '-', direction: null, error: null,
    },
    controls: controls({
      start: true, exit: true, jog_hold: true, distributor_diagnostic: true,
    }),
  });
  api.updateLineStatus(currentStatus);
  api.state.currentCamera = null;
  api.selectCamera('TOP');
  await sleep(30);
  const thresholdsBody = window.document.getElementById('thresholds-body');
  assert(
    thresholdsBody.querySelectorAll('.thresholds-card').length === 2,
    'каждое правило — отдельный блок-карточка',
  );
  const thresholdInputs = thresholdsBody.querySelectorAll('input.thresholds-input');
  assert(thresholdInputs.length === 3, 'у каждого порога есть числовое поле');
  assert(
    thresholdsBody.querySelectorAll('.thresholds-item input[type="range"]').length === 0,
    'внутри строк порогов нет ползунков значений',
  );
  // Одновременно видна одна карточка; у неё собственный вертикальный
  // ползунок прокрутки строк (сверху вниз).
  const activeCard = thresholdsBody.querySelector('.thresholds-card.is-active');
  assert(!!activeCard, 'видна активная карточка правил');
  assert(
    activeCard.querySelectorAll('.thresholds-rows .thresholds-item').length === 2,
    'в активной карточке собраны строки параметров',
  );
  const activeCardSlider = activeCard.querySelector('input.thresholds-scroll-slider');
  assert(!!activeCardSlider, 'у карточки есть собственный ползунок');
  assert(
    activeCard.classList.contains('is-expanded'),
    'выбранная карточка правила развёрнута',
  );

  // jsdom не делает раскладку: подставляем размеры прокрутки строк, чтобы
  // проверить, что ползунок активен и двигает строки своей карточки.
  const activeRows = activeCard.querySelector('.thresholds-rows');
  const scrollState = {top: 0};
  Object.defineProperty(activeRows, 'scrollHeight', {value: 1000, configurable: true});
  Object.defineProperty(activeRows, 'clientHeight', {value: 400, configurable: true});
  Object.defineProperty(activeRows, 'scrollTop', {
    configurable: true,
    get: () => scrollState.top,
    set: value => { scrollState.top = value; },
  });
  activeRows.dispatchEvent(new window.Event('scroll'));
  assert(!activeCardSlider.disabled, 'ползунок активен, когда строки не помещаются');
  assert(
    !activeCardSlider.classList.contains('is-idle'),
    'при переполнении ползунок видим',
  );
  activeCardSlider.value = '500';
  activeCardSlider.dispatchEvent(new window.Event('input', {bubbles: true}));
  assert(scrollState.top === 300, 'ползунок прокручивает строки карточки');

  // Переключение между правилами — заголовки-вкладки, как в браузере:
  // у следующей карточки одна строка — ползунок не нужен и остаётся
  // отключённым.
  const tabs = thresholdsBody.querySelectorAll('.thresholds-tab');
  assert(tabs.length === 2, 'у каждого правила есть вкладка-заголовок');
  assert(
    tabs[0].classList.contains('is-active'),
    'первая вкладка активна по умолчанию',
  );
  assert(
    tabs[0].getAttribute('aria-selected') === 'true',
    'активная вкладка отмечена aria-selected',
  );
  assert(
    tabs[0].textContent.includes('ГЕОМЕТРИЯ ОКНА'),
    'вкладка несёт название правила',
  );
  assert(
    thresholdsBody.querySelectorAll('.thresholds-nav').length === 0,
    'стрелочной навигации больше нет',
  );
  tabs[1].click();
  assert(
    thresholdsBody.querySelector('.thresholds-card.is-active').dataset.rule === 'input_part_presence',
    'клик по вкладке переключает на её карточку',
  );
  assert(tabs[1].classList.contains('is-active'), 'вкладка подсвечивается');
  assert(
    tabs[1].getAttribute('aria-selected') === 'true',
    'aria-selected переходит на активную вкладку',
  );
  assert(
    tabs[1].title.includes('НАЛИЧИЕ КОРПУСА'),
    'активная вкладка несёт полное название правила',
  );
  assert(
    tabs[0].getAttribute('aria-selected') === 'false',
    'прежняя вкладка снимается',
  );

  // Наведение «разъезжает» заголовок: вкладка раскрывается до полного
  // названия (JS измеряет ширину текста и подставляет во flex-basis);
  // переход на элемент внутри вкладки раскрытие не отменяет, а уход с
  // вкладки возвращает ленту к равным долям.
  const tabsBar = thresholdsBody.querySelector('.thresholds-tabs');
  const hoverTab = tabsBar.querySelector('.thresholds-tab');
  const hoverLabel = hoverTab.querySelector('.thresholds-tab-label');
  const hoverCount = hoverTab.querySelector('.thresholds-tab-count');
  Object.defineProperty(hoverLabel, 'scrollWidth', {value: 200, configurable: true});
  hoverTab.dispatchEvent(new window.MouseEvent('pointerover', {bubbles: true}));
  assert(
    hoverTab.style.flexBasis === '218px',
    'наведение раскрывает заголовок до полного названия',
  );
  hoverTab.dispatchEvent(new window.MouseEvent('pointerout', {
    bubbles: true,
    relatedTarget: hoverCount,
  }));
  assert(
    hoverTab.style.flexBasis === '218px',
    'переход на счётчик внутри вкладки не сворачивает заголовок',
  );
  hoverTab.dispatchEvent(new window.MouseEvent('pointerout', {
    bubbles: true,
    relatedTarget: tabsBar,
  }));
  assert(
    hoverTab.style.flexBasis === '',
    'уход с вкладки возвращает ленту к равным долям',
  );
  const secondSlider = thresholdsBody.querySelector(
    '.thresholds-card.is-active input.thresholds-scroll-slider',
  );
  assert(secondSlider.disabled, 'карточка без переполнения ползунок не показывает');
  assert(
    secondSlider.classList.contains('is-idle'),
    'когда все строки видны, ползунок полностью скрыт',
  );
  assert(
    thresholdsBody.querySelector('.thresholds-card.is-active')
      .classList.contains('is-expanded'),
    'карточка выбранного правила разворачивается при переключении вкладок',
  );

  // Клик по первой вкладке возвращает к первой карточке; её ползунок
  // снова синхронизируется с сохранённой прокруткой строк.
  tabs[0].click();
  assert(
    thresholdsBody.querySelector('.thresholds-card.is-active').dataset.rule === 'input_window_geometry',
    'вкладка возвращает на первую карточку',
  );
  assert(tabs[0].classList.contains('is-active'), 'первая вкладка снова активна');
  assert(
    thresholdsBody.querySelector(
      '.thresholds-card.is-active input.thresholds-scroll-slider',
    ).disabled === false,
    'ползунок первой карточки снова активен после возврата',
  );

  // Повторный клик по активной вкладке сворачивает карточку правила,
  // ещё один клик — разворачивает обратно.
  tabs[0].click();
  assert(
    !thresholdsBody.querySelector('.thresholds-card.is-active')
      .classList.contains('is-expanded'),
    'клик по активной вкладке сворачивает карточку',
  );
  tabs[0].click();
  assert(
    thresholdsBody.querySelector('.thresholds-card.is-active')
      .classList.contains('is-expanded'),
    'повторный клик по активной вкладке разворачивает карточку',
  );

  // Значение задаётся числовым полем; при активном JOG-режиме
  // редактирование остаётся доступным (jog.busy === false).
  const firstThresholdInput = thresholdInputs[0];
  firstThresholdInput.value = '0.75';
  firstThresholdInput.dispatchEvent(new window.Event('input', {bubbles: true}));
  assert(
    !window.document.getElementById('thresholds-save').disabled,
    'после ввода значения СОХРАНИТЬ доступна даже при активном JOG',
  );

  // СОХРАНИТЬ: POST /api/thresholds с новым значением, ответ применяется,
  // статус «Сохранено», после сохранения кнопка снова заблокирована.
  calls.length = 0;
  window.document.getElementById('thresholds-save').click();
  await sleep(30);
  const thresholdsSaveCall = calls.find(call => (
    call.url === '/api/thresholds'
    && (call.options.method || 'GET') === 'POST'
  ));
  assert(!!thresholdsSaveCall, 'СОХРАНИТЬ отправляет POST /api/thresholds');
  assert(
    JSON.parse(thresholdsSaveCall.options.body).values.min_confidence === 0.75,
    'POST содержит изменённое значение',
  );
  assert(
    window.document.getElementById('thresholds-status').textContent === 'Сохранено',
    'статус «Сохранено» показывается после ответа backend',
  );
  assert(
    thresholdsBody.querySelector('input.thresholds-input').value === '0.75',
    'поля перестроены по ответу backend с новым значением',
  );
  assert(
    window.document.getElementById('thresholds-save').disabled,
    'после сохранения изменений нет — СОХРАНИТЬ снова заблокирована',
  );

  // На устройствах без наведения (тачскрины, matchMedia hover: none)
  // раскрытие заголовков не подключается: лента не дёргается, полное
  // название остаётся в подсказке title. Возможность наведения
  // проверяется при построении ленты, поэтому перестраиваем её уже
  // с новым matchMedia (смена камеры перечитывает пороги и строит
  // ленту заново).
  window.matchMedia = () => ({
    matches: false, media: '', onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {},
    removeEventListener() {}, dispatchEvent() { return false; },
  });
  api.selectCamera('INPUT_LEFT');
  await sleep(30);
  const touchTab = thresholdsBody.querySelector('.thresholds-tab');
  touchTab.dispatchEvent(new window.MouseEvent('pointerover', {bubbles: true}));
  assert(
    touchTab.style.flexBasis === '',
    'без hover раскрытие заголовков не включается',
  );
  touchTab.dispatchEvent(new window.MouseEvent('pointerout', {bubbles: true}));
  assert(
    touchTab.style.flexBasis === '',
    'без hover уход с вкладки ничего не меняет',
  );

  console.log('UI INTERACTION MATRIX PASS: 23 groups');
  dom.window.close();
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
