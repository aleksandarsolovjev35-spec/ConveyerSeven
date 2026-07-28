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
    exit: true,
    pause: false,
    resume: false,
    nudge: false,
    nudge_hold: false,
    jog_hold: false,
    selected_model_analysis: false,
    selected_model_release: false,
    distributor_diagnostic: false,
    camera_diagnostic: false,
    vision_rule_diagnostic: false,
    ...overrides,
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
          {name: 'rule_ok', triggered: false, skipped: false, detail: 'Норма'},
          {name: 'rule_bad', triggered: true, skipped: false, detail: 'Сработало'},
        ],
        updated_at: Date.now() / 1000,
      };
      currentStatus.frame_analysis = {
        available: true,
        kind: 'SELECTED',
        active: true,
        title: 'АНАЛИЗ 3 КАДРОВ',
        role,
        message: currentStatus.diagnostics.message,
        models: currentStatus.diagnostics.models,
        rules: currentStatus.diagnostics.rules,
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
  const viewModeButtons = [
    window.document.getElementById('view-mode-raw'),
    window.document.getElementById('view-mode-rules'),
  ];
  const hidden = element => element.classList.contains('is-hidden');

  // 1. No backend state: all controls remain fail-closed.
  assert(start.disabled && stop.disabled && exit.disabled, 'initial main controls disabled');
  assert(diagnostics.every(button => button.disabled), 'initial diagnostics disabled');
  assert(jogButtons.every(button => button.disabled), 'initial JOG disabled');
  assert(analyzeSelected.disabled, 'initial selected-frame analysis disabled');
  assert(viewModeButtons.every(button => button.disabled), 'initial RAW/RULES controls disabled');

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
  assert(viewModeButtons.every(button => button.disabled), 'IDLE RAW/RULES disabled before frame analysis');
  assert(window.document.getElementById('camera-view-switch').classList.contains('is-faded'), 'IDLE RAW/RULES hidden before frame analysis');
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
  assert(viewModeButtons.every(button => !button.disabled), 'RUNNING RAW/RULES enabled');

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
  api.updateLineStatus(line);
  const cells = [...window.document.querySelectorAll('#line-cells .line-cell')];
  assert(cells[0].textContent === '№1', 'INPUT part ID rendered');
  assert(cells[4].textContent === '№2' && cells[4].classList.contains('process-camera'), 'SPIDER part highlighted');
  assert(cells[4].querySelector('.line-process-interval.process-analysis.is-active'), 'SPIDER analysis interval highlighted');
  assert(cells[7].textContent === '№3' && cells[7].classList.contains('cell-cleanup'), 'ROUTE cleanup rendered');
  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    line_parts: [{id: 2, position: 4, category: 'BAD'}],
    process: {
      phase: 'CONVEYOR_MOVING', label: 'belt moves', positions: [0,1,2,3,4,5,6,7],
      conveyor: {pos: 50, tgt: 100, mov: 1, wait: 0},
    },
  }));
  assert(window.document.querySelector('#line-cells .line-cell.occupied.belt-moving'), 'occupied part animates with conveyor movement');
  assert(window.document.querySelector('#line-cells .line-cell.empty-slot.belt-moving'), 'empty slots show conveyor movement too');
  api.updateLineStatus(line);
  api.updateRecentParts([{id: 2, category: 'BAD', decision: 'contacts'}]);
  assert(window.document.getElementById('defects-title').textContent === 'ОСНОВНЫЕ ДЕФЕКТЫ', 'top defects shown while running');
  assert(!window.document.getElementById('defects-section').classList.contains('is-hidden'), 'working defects section visible');

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
  assert(viewModeButtons.every(button => button.disabled), 'RAW/RULES disabled before analysis');
  assert(window.document.getElementById('camera-view-switch').classList.contains('is-faded'), 'RAW/RULES hidden before analysis');

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
  calls.length = 0;
  analyzeSelected.click();
  await sleep(45);
  assert(calls.some(call => call.url.endsWith('/api/diagnostics/selected/SPIDER_IN')), 'selected model endpoint');
  assert(analyzeSelected.textContent === 'ВЕРНУТЬ ПОТОК', 'analysis button becomes return-live');
  assert(window.document.getElementById('mode-badge').textContent === 'АНАЛИЗ', 'live badge is replaced during analysis');
  assert(window.document.getElementById('main-camera').src.includes('mode=RULES'), 'selected rule overlay shown');
  assert(!window.document.getElementById('frame-analysis-panel').classList.contains('is-collapsed'), 'selected frame analysis replaces right panel');
  assert(window.document.getElementById('stats-summary').classList.contains('is-collapsed'), 'statistics hidden while selected frame is frozen');
  assert(window.document.getElementById('distributor-diagnostics').classList.contains('is-collapsed'), 'distributor hidden while selected frame is frozen');
  assert(window.document.getElementById('jog-panel').classList.contains('is-collapsed'), 'manual controls hidden while selected frame is frozen');
  assert(window.document.querySelectorAll('#frame-analysis-models .frame-analysis-item').length === 2, 'selected models listed');
  assert(window.document.querySelectorAll('#frame-analysis-rules .frame-analysis-item').length === 2, 'selected rules listed');
  assert(viewModeButtons.every(button => !button.disabled), 'RAW/RULES enabled during selected analysis');
  calls.length = 0;
  await api.setViewMode('RAW');
  assert(calls.some(call => call.url === '/api/mode/RAW'), 'RAW enabled during selected analysis');
  assert(window.document.getElementById('main-camera').src.includes('mode=RAW'), 'selected RAW overlay shown');
  assert(window.document.getElementById('defects-section').classList.contains('is-hidden'), 'old statistics analysis summary is not used');
  const frozenCamera = window.document.getElementById('camera-label').textContent;
  api.selectCamera('TOP');
  assert(window.document.getElementById('camera-label').textContent === frozenCamera, 'camera switch blocked during frozen analysis');
  assert(start.disabled && diagnostics.every(button => button.disabled), 'selected analysis blocks START and distributor');
  assert(jogButtons.every(button => button.disabled), 'selected analysis blocks JOG');
  analyzeSelected.click();
  await sleep(45);
  assert(calls.some(call => call.url === '/api/diagnostics/selected/release'), 'return LIVE endpoint');
  assert(analyzeSelected.textContent === 'АНАЛИЗ 3 КАДРОВ', 'analysis button restored');
  assert(window.document.getElementById('mode-badge').textContent.includes('ПОТОК'), 'live badge restored');
  assert(window.document.getElementById('frame-analysis-panel').classList.contains('is-collapsed'), 'selected analysis is cleared after returning to live');
  assert(!window.document.getElementById('stats-summary').classList.contains('is-collapsed'), 'statistics restored after selected analysis');
  assert(!window.document.getElementById('distributor-diagnostics').classList.contains('is-collapsed'), 'distributor restored after selected analysis');
  assert(!window.document.getElementById('jog-panel').classList.contains('is-collapsed'), 'manual controls restored after selected analysis');
  assert(window.document.getElementById('camera-view-switch').classList.contains('is-faded'), 'RAW/RULES hidden after selected analysis');

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
  assert(window.document.getElementById('gallery-grid').textContent.includes('Деталь не найдена'), 'missing archive shown');
  api.closeGallery();

  // 17. API command error is visible and a later explicit action may clear it.
  api.showControlError('START rejected');
  assert(!window.document.getElementById('control-error').classList.contains('is-hidden'), 'control error visible');
  assert(window.document.getElementById('control-error').textContent.includes('START rejected'), 'control error text');
  api.clearControlError();
  assert(window.document.getElementById('control-error').classList.contains('is-hidden'), 'control error clears explicitly');
  api.updateLineStatus({});
  assert(start.disabled && exit.disabled, 'malformed status keeps main controls disabled');
  // Повреждённый ответ backend не должен ронять отрисовку: исключение
  // в updateLineCells остановило бы и обновление статуса, и привод.
  for (const broken of [
    {state: 'RUNNING', line_parts: 'nope', process: {phase: 'X', positions: 'nope', conveyor: {}}},
    {state: 'RUNNING', line_parts: [{position: 0}, {id: null}, {id: 7}], process: {phase: 'X', conveyor: {}}},
    {state: 'RUNNING', step: NaN, dist1_position: Infinity, dist1_max: NaN,
     process: {phase: 'CONVEYOR_MOVING', conveyor: {pos: NaN, tgt: Infinity}}, pause: {nudge_offset: NaN}},
    {state: 'RUNNING', step: 1, process: {phase: 'CONVEYOR_MOVING', conveyor: {pos: 50, tgt: 0}}},
  ]) {
    api.updateLineStatus(broken);
    for (const variable of ['--belt-phase', '--belt-intra', '--belt-speed', '--drive-turn', '--dist1-turn']) {
      const value = api.getBeltVar(variable);
      assert(
        value !== '' && !/NaN|Infinity|undefined/.test(value),
        `malformed status keeps ${variable} finite (got "${value}")`,
      );
    }
  }
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

  // 19. PAUSE inside the cycle exposes bounded belt correction only.
  const btnPause = window.document.getElementById('btn-pause');
  const btnResume = window.document.getElementById('btn-resume');
  const nudgePanel = window.document.getElementById('nudge-panel');
  const nudgeForward = window.document.getElementById('nudge-forward');
  const nudgeBackward = window.document.getElementById('nudge-backward');

  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true, pause: true}),
  }));
  assert(!hidden(btnPause) && !btnPause.disabled, 'RUNNING exposes PAUSE');
  assert(hidden(btnResume), 'RUNNING hides RESUME');
  assert(nudgePanel.classList.contains('is-collapsed'), 'RUNNING hides belt correction');

  const pauseStatus = (overrides = {}) => lineStatus('PAUSED', {
    controls: controls({
      stop: true, exit: true, resume: true, nudge: true, nudge_hold: true,
      ...(overrides.controls || {}),
    }),
    pause: {
      requested: false,
      active: true,
      nudge_offset: 0,
      nudge_limit_steps: 5000,
      micro_steps: 500,
      remaining_forward: 5000,
      remaining_backward: 5000,
      hold_busy: false,
      hold_direction: null,
      hold_speed: 20000,
      ...(overrides.pause || {}),
    },
  });

  currentStatus = pauseStatus();
  api.updateLineStatus(currentStatus);
  assert(window.document.getElementById('state-label').textContent === 'ПАУЗА · КОРРЕКЦИЯ ЛЕНТЫ', 'PAUSED label is explicit');
  assert(hidden(btnPause) && !hidden(btnResume) && !btnResume.disabled, 'PAUSED swaps PAUSE for RESUME');
  assert(!hidden(stop) && !stop.disabled, 'PAUSED still allows normal STOP');
  assert(!nudgePanel.classList.contains('is-collapsed'), 'PAUSED reveals belt correction');
  assert(!nudgeForward.disabled && !nudgeBackward.disabled, 'PAUSED enables both correction directions');
  assert(jogButtons.every(button => button.disabled), 'PAUSED keeps continuous JOG locked');

  // Held correction must behave like JOG: start, heartbeat and release.
  async function nudgeHoldScenario(releaseEvent) {
    api.clearNudgeHoldLocalState();
    currentStatus = pauseStatus();
    api.updateLineStatus(currentStatus);
    calls.length = 0;
    nudgeForward.dispatchEvent(new window.Event('pointerdown', {bubbles: true}));
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
      nudgeForward.dispatchEvent(event);
    }
    await sleep(10);
    assert(calls.some(call => call.url === '/api/nudge/hold/start'), `correction hold start for ${releaseEvent}`);
    assert(calls.some(call => call.url === '/api/nudge/hold/heartbeat'), `correction heartbeat for ${releaseEvent}`);
    assert(calls.some(call => call.url === '/api/nudge/hold/release'), `correction release for ${releaseEvent}`);
    api.clearNudgeHoldLocalState();
  }
  for (const event of [
    'pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave',
    'blur', 'visibilitychange', 'pagehide', 'beforeunload',
  ]) {
    await nudgeHoldScenario(event);
  }

  // While the belt moves, the opposite direction must not be pressable.
  api.clearNudgeHoldLocalState();
  api.updateLineStatus(pauseStatus({
    pause: {hold_busy: true, hold_direction: '+'},
  }));
  assert(nudgeBackward.disabled, 'moving belt locks the opposite correction button');
  assert(nudgeForward.classList.contains('nudge-active'), 'active correction direction is highlighted');

  // Reaching the accumulated limit must disable that direction only.
  api.clearNudgeHoldLocalState();
  api.updateLineStatus(pauseStatus({
    pause: {
      nudge_offset: 5000,
      remaining_forward: 0,
      remaining_backward: 10000,
    },
  }));
  assert(nudgeForward.disabled, 'correction limit disables further forward moves');
  assert(!nudgeBackward.disabled, 'correction limit still allows returning back');
  assert(window.document.getElementById('nudge-offset').textContent === '+5000', 'accumulated correction is shown');

  // Leaving the pause must abort any local hold state.
  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true, pause: true}),
  }));
  assert(nudgePanel.classList.contains('is-collapsed'), 'RESUME hides belt correction again');
  api.updateLineStatus(pauseStatus());

  // Arrow keys must hold the bounded correction, exactly like in JOG.
  api.clearNudgeHoldLocalState();
  currentStatus = pauseStatus();
  api.updateLineStatus(currentStatus);
  calls.length = 0;
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  await sleep(10);
  window.dispatchEvent(new window.KeyboardEvent('keyup', {key: 'ArrowRight', bubbles: true}));
  await sleep(10);
  assert(calls.some(call => call.url === '/api/nudge/hold/start'), 'keyboard correction start');
  assert(calls.some(call => call.url === '/api/nudge/hold/heartbeat'), 'keyboard correction heartbeat');
  assert(calls.some(call => call.url === '/api/nudge/hold/release'), 'keyboard correction release');
  assert(!calls.some(call => call.url.startsWith('/api/active_camera')), 'paused arrows never switch cameras');

  // A direction with an exhausted budget must not be reachable by keyboard.
  api.clearNudgeHoldLocalState();
  api.updateLineStatus(pauseStatus({
    pause: {nudge_offset: 5000, remaining_forward: 0, remaining_backward: 10000},
  }));
  calls.length = 0;
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  await sleep(10);
  assert(!calls.some(call => call.url === '/api/nudge/hold/start'), 'keyboard respects the exhausted budget');
  window.dispatchEvent(new window.KeyboardEvent('keyup', {key: 'ArrowRight', bubbles: true}));
  await sleep(5);

  // Key auto-repeat must not open a second hold on top of the running one.
  api.clearNudgeHoldLocalState();
  api.updateLineStatus(pauseStatus());
  calls.length = 0;
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true}));
  await sleep(10);
  window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true, repeat: true}));
  await sleep(10);
  assert(calls.filter(call => call.url === '/api/nudge/hold/start').length === 1, 'key repeat does not restart the hold');
  window.dispatchEvent(new window.KeyboardEvent('keyup', {key: 'ArrowLeft', bubbles: true}));
  await sleep(10);
  api.clearNudgeHoldLocalState();
  api.updateLineStatus(pauseStatus());

  calls.length = 0;
  btnResume.click();
  await sleep(5);
  assert(calls.some(call => call.url === '/api/resume'), 'RESUME returns the line to work');

  // 20. Splash startup error has an explicit close action.
  calls.length = 0;
  window.document.getElementById('splash-exit').disabled = false;
  window.document.getElementById('splash-exit').click();
  await sleep(5);
  assert(calls.some(call => call.url === '/api/exit'), 'splash close calls EXIT');

  // 21. Every animation is driven by real conveyor telemetry.
  // Движение в интерфейсе разрешено только тогда, когда линия реально
  // движется, и обязано останавливаться вместе с ней.
  const beltVar = name => api.getBeltVar(name);
  const beltRunning = () => window.document.body.classList.contains('belt-running');
  const beltReadout = () => window.document.getElementById('belt-readout').textContent;

  api.updateLineStatus(lineStatus('IDLE', {controls: controls({start: true, exit: true})}));
  let clock = 10000;
  api.advanceBelt(clock);
  // Предыдущие сценарии оставили ленту в движении коррекции: она обязана
  // доехать до подтверждённой позиции и только потом замереть.
  for (let frame = 0; frame < 90; frame++) {
    clock += 16;
    api.advanceBelt(clock);
  }
  const idlePhase = beltVar('--belt-phase');
  clock += 16;
  api.advanceBelt(clock);
  assert(beltVar('--belt-phase') === idlePhase, 'idle line settles to the real position');
  assert(!beltRunning(), 'idle line keeps the interface still');
  assert(beltReadout() === 'ПРИВОД · СТОП', 'idle drive readout');

  // Ход ленты в цикле: каждый кадр между опросами двигается.
  api.updateLineStatus(lineStatus('RUNNING', {
    step: 3,
    controls: controls({stop: true, exit: true}),
    process: {
      phase: 'CONVEYOR_MOVING', label: 'belt moves', positions: [0],
      conveyor: {pos: 0, tgt: 100, mov: 1, wait: 0},
    },
  }));
  clock += 16;
  api.advanceBelt(clock);
  let movingFrames = 0;
  let totalFrames = 0;
  for (let poll = 1; poll <= 8; poll++) {
    api.updateLineStatus(lineStatus('RUNNING', {
      step: 3,
      controls: controls({stop: true, exit: true}),
      process: {
        phase: 'CONVEYOR_MOVING', label: 'belt moves', positions: [0],
        conveyor: {pos: poll * 12, tgt: 100, mov: 1, wait: 0},
      },
    }));
    for (let frame = 0; frame < 6; frame++) {
      clock += 16;
      if (api.advanceBelt(clock)) movingFrames++;
      totalFrames++;
    }
  }
  assert(movingFrames === totalFrames, 'belt travel animates on every frame between polls');
  assert(beltRunning(), 'moving belt marks the interface as running');
  assert(Number(beltVar('--belt-speed')) > 0, 'belt speed variable follows the controller');
  assert(Number(beltVar('--belt-intra')) > 0, 'part path offset follows the step progress');
  assert(beltReadout().startsWith('ПРИВОД · ХОД'), 'cycle travel readout shows real progress');

  // Остановка ленты обязана останавливать интерфейс.
  api.updateLineStatus(lineStatus('RUNNING', {
    step: 4,
    controls: controls({stop: true, exit: true}),
    process: {phase: 'STEP_COMPLETE', label: 'step done', positions: [], conveyor: {}},
  }));
  for (let frame = 0; frame < 60; frame++) {
    clock += 16;
    api.advanceBelt(clock);
  }
  const restingPhase = beltVar('--belt-phase');
  clock += 16;
  api.advanceBelt(clock);
  assert(beltVar('--belt-phase') === restingPhase, 'stopped belt freezes the interface');
  assert(!beltRunning(), 'stopped belt clears the running marker');

  // Ручное удержание двигает ленту, отпускание — останавливает.
  api.updateLineStatus(lineStatus('IDLE', {
    controls: controls({start: true, exit: true, jog_hold: true}),
    jog: {...currentStatus.jog, active: true, busy: true, direction: '+'},
  }));
  // Первый кадр подхватывает новый счётчик шага, а не рисует ход ленты.
  clock += 16;
  api.advanceBelt(clock);
  const jogPhaseBefore = Number(beltVar('--belt-phase'));
  for (let frame = 0; frame < 30; frame++) {
    clock += 16;
    api.advanceBelt(clock);
  }
  assert(Number(beltVar('--belt-phase')) > jogPhaseBefore, 'JOG hold drives the belt forward');
  assert(beltReadout() === 'ПРИВОД · РУЧНОЙ ХОД', 'manual travel readout');

  api.updateLineStatus(lineStatus('IDLE', {
    controls: controls({start: true, exit: true, jog_hold: true}),
    jog: {...currentStatus.jog, active: true, busy: false, direction: null},
  }));
  for (let frame = 0; frame < 60; frame++) {
    clock += 16;
    api.advanceBelt(clock);
  }
  const jogRest = beltVar('--belt-phase');
  clock += 16;
  api.advanceBelt(clock);
  assert(beltVar('--belt-phase') === jogRest, 'released JOG stops the interface too');

  // Коррекция в паузе двигает ленту ровно на подтверждённые микрошаги.
  api.updateLineStatus(pauseStatus({pause: {hold_busy: true, hold_direction: '+'}}));
  const nudgePhase = Number(beltVar('--belt-phase'));
  api.updateLineStatus(pauseStatus({
    pause: {hold_busy: true, hold_direction: '+', nudge_offset: 2000},
  }));
  for (let frame = 0; frame < 40; frame++) {
    clock += 16;
    api.advanceBelt(clock);
  }
  assert(Number(beltVar('--belt-phase')) > nudgePhase, 'belt correction moves the interface');
  assert(beltReadout() === 'ПРИВОД · КОРРЕКЦИЯ', 'correction travel readout');

  // Оси распределителя вращают кулачок ровно по своим координатам.
  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    dist1_position: 170, dist1_max: 340, dist1_state: 'MOVING',
    dist2_position: 340, dist2_max: 340, dist2_state: 'IDLE',
  }));
  assert(Number(beltVar('--dist1-turn')) === 37, 'DIST1 cam angle follows the axis coordinate');
  assert(Number(beltVar('--dist2-turn')) === 74, 'DIST2 cam angle follows the axis coordinate');
  assert(window.document.getElementById('dist1-card').classList.contains('is-moving'), 'moving blade is marked');
  assert(!window.document.getElementById('dist2-card').classList.contains('is-moving'), 'resting blade is still');

  // Съёмка, анализ и сортировка отражают текущую фазу шага.
  const cameraContainer = window.document.querySelector('.camera-container');
  const phaseCases = [
    ['CAMERA_CAPTURE', 'is-capturing'],
    ['SPIDER_ANALYSIS', 'is-analyzing'],
    ['ROUTE_DROP', 'is-routing'],
  ];
  for (const [phase, marker] of phaseCases) {
    api.updateLineStatus(lineStatus('RUNNING', {
      controls: controls({stop: true, exit: true}),
      process: {phase, label: phase, positions: [0], conveyor: {}},
    }));
    assert(cameraContainer.classList.contains(marker), `${phase} drives ${marker}`);
  }
  api.updateLineStatus(lineStatus('RUNNING', {
    controls: controls({stop: true, exit: true}),
    process: {phase: 'STEP_COMPLETE', label: 'idle', positions: [], conveyor: {}},
  }));
  assert(
    phaseCases.every(([, marker]) => !cameraContainer.classList.contains(marker)),
    'finished step stops the frame animations',
  );

  // Ленты и слои эффектов существуют и не рисуются inline-стилями.
  assert(window.document.querySelectorAll('.belt-lane-strip').length >= 6, 'belt lanes are built');
  assert(window.document.querySelectorAll('#line-cells .line-cell-fx').length === 8, 'every cell has an event layer');
  assert(
    [...window.document.querySelectorAll('.line-cell-main')].every(el => !el.style.transform),
    'part path movement comes from belt variables, not inline styles',
  );

  // Потеря связи замораживает интерфейс: движения без телеметрии нет.
  api.markUiOffline();
  for (let frame = 0; frame < 30; frame++) {
    clock += 16;
    api.advanceBelt(clock);
  }
  assert(!beltRunning(), 'offline backend freezes every animation');
  assert(beltReadout() === 'ПРИВОД · СТОП', 'offline drive readout');
  api.state.offline = false;
  window.document.getElementById('main').classList.remove('ui-offline');

  console.log('UI INTERACTION MATRIX PASS: 22 groups');
  dom.window.close();
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
