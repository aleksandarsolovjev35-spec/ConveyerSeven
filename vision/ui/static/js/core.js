// core.js — Line Monitor UI module (reworked for stability)
// - устранены гонки в поллинге статуса и загрузке кадров
// - mainBuffer защищён счётчиком последовательности
// - адаптивный polling на setTimeout вместо setInterval
'use strict';

// ─── Config ──────────────────────────────────────────────────

const BOOT_INTERVAL        = 150;
const STATUS_INTERVAL_FAST   = 100;
const STATUS_INTERVAL_MOTION = 60;
const STATUS_INTERVAL_IDLE   = 500;
const STATUS_OFFLINE_AFTER   = 1500;
const PREVIEW_INTERVAL       = 180;
const UPTIME_INTERVAL      = 1000;
const MAIN_CAM_MIN_GAP     = 16;
const LIVE_CAM_MIN_GAP     = 1000 / 30;

const JOG_ALLOWED_STATES = ["IDLE", "STOPPED", "PAUSED"];
const JOG_HEARTBEAT_INTERVAL = 100;
const EXPECTED_CAMERAS = 7;

const CAMERA_ROLE_LABELS = {
    INPUT_LEFT:   'ВХОД · СЛЕВА',
    INPUT_RIGHT:  'ВХОД · СПРАВА',
    SPIDER_LEFT:  'КОНТРОЛЬ · СЛЕВА',
    SPIDER_RIGHT: 'КОНТРОЛЬ · СПРАВА',
    SPIDER_IN:    'ВНУТРЕННИЙ ВИД',
    SPIDER_OUT:   'НАРУЖНЫЙ ВИД',
    TOP:          'ВИД СВЕРХУ',
};

const LINE_STATE_LABELS = {
    IDLE: 'ГОТОВА К ПУСКУ',
    RUNNING: 'РАБОТАЕТ',
    PAUSED: 'ПАУЗА · КОРРЕКЦИЯ ЛЕНТЫ',
    STOPPING: 'ОСТАНОВКА ЛИНИИ',
    STOPPED: 'ОСТАНОВЛЕНА',
    FAULT: 'АВАРИЯ',
    OFFLINE: 'НЕТ СВЯЗИ',
};

const AXIS_STATE_LABELS = {
    IDLE: 'В ПОЗИЦИИ',
    READY: 'В ПОЗИЦИИ',
    WAITING: 'ОЖИДАНИЕ',
    HOMING: 'ПОИСК НУЛЯ',
    MOVING: 'ПЕРЕМЕЩЕНИЕ',
    MOVING_TO_GOOD: 'К ГОДНОМУ',
    GOOD: 'ГОДНО',
    MOVING_TO_DIST2: 'НА DIST2',
    TO_DIST2: 'НА DIST2',
    FAULT: 'АВАРИЯ',
};

const CATEGORY_LABELS = {
    GOOD: 'ГОДНО',
    BAD: 'БРАК',
    CLEANUP: 'НА ОЧИСТКУ',
    UNKNOWN: 'НЕ ОПРЕДЕЛЕНО',
};

const UI_READY_TIMEOUT   = 20000;
const UI_READY_CHECK_INT = 100;

// ─── State ───────────────────────────────────────────────────

const state = {
    cameras:             [],
    currentCamera:       null,
    mode:                'RULES',
    modePending:         false,
    startTime:           Date.now(),
    splashActive:        true,
    lastFrameTime:       0,
    lineState:           'IDLE',
    serverExitRequested: false,
    lastSeenVersion:     -1,
    currentVersion:      0,
    frameVersions:       {},
    bootDone:            false,
    bootDoneAt:          0,
    cameraHovered:       false,
    statusInterval:      null,
    bootInterval:        null,
    bootFetchBusy:       false,
    statusFetchBusy:     false,
    camerasFetchBusy:    false,

    statusReceived:      false,
    jogReceived:         false,
    uiReady:             false,
    uiRevealed:          false,

    jogActive:           false,
    jogBusy:             false,
    jogTogglePending:    false,
    jogHoldDirection:    null,
    jogHeartbeatTimer:   null,
    jogHeartbeatBusy:    false,
    jogReleasePending:   false,
    jogStartPromise:     null,
    distributorDiagnosticPending: false,
    distributorDiagnosticBackendBusy: false,

    selectedAnalysisActive: false,
    selectedAnalysisRole:   null,
    selectedAnalysisPending: false,

    frameAnalysisRulesCache: null,
    frameAnalysisRulesFilter: 'triggered',
    frameAnalysisModelsCache: null,
    lastFrameAnalysisRenderKey: null,

    liveFps:              0.0,
    // Backend сообщает целевой режим, но изображение меняется асинхронно.
    // Бейдж использует displayedFrameKind — режим кадра, уже загруженного в
    // главное окно, а не только состояние камеры на сервере.
    liveStreaming:        false,
    liveStatic:           false,
    displayedFrameKind:   null, // 'live' | 'static' | 'analysis' | null
    pendingDisplayKind:   null,
    pendingDisplaySeq:    0,
    controlPending:       false,
    startPending:         false,
    backendControls:      {},
    offline:              false,
    lastStatusAt:         0,

    lastSentActiveCamera: null,
    pendingActiveCamera:  null,
    activeCameraRequestBusy: false,
    thresholdsRevision:   null,

    pictureRun: 0,
    mainCamMode:          'pull',
    mainCamStreamRole:    null,
    mainCamStreamView:    null,
    mainCamAnalysisKey:   null,
    livePullTimer:        null,

    pendingAnalysisVersion: null,
    pendingFlushTimer:      null,
    lastLineStatus:         null,
};

// ─── Gallery state ───────────────────────────────────────────

let galleryMode   = 'debug';
let galleryPartId = null;
let galleryData   = null;

// ─── DOM cache ───────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

const els = {
    splash:           $('splash'),
    splashMessage:    $('splash-message'),
    splashProgress:   $('splash-progress-fill'),
    splashError:      $('splash-error'),
    splashErrorMsg:   $('splash-error-message'),
    splashExit:       $('splash-exit'),

    main:             $('main'),

    stateSection:     $('state-section'),
    stateIndicator:   $('state-indicator'),
    stateLabel:       $('state-label'),
    metricStep:       $('metric-step'),
    metricUptime:     $('metric-uptime'),

    previewStrip:     $('preview-strip'),
    cameraLabel:      $('camera-label'),
    modeBadge:        $('mode-badge'),
    mainCamera:       $('main-camera'),
    cameraOverlay:    $('camera-overlay'),
    viewModeToggle:   $('view-mode-toggle'),
    analyzeSelectedFrame: $('analyze-selected-frame'),
    cameraContainer:  null,

    dist1State:       $('dist1-state'),
    dist1Pos:         $('dist1-pos'),
    dist1Max:         $('dist1-max'),
    dist1Blade:       $('dist1-blade'),
    dist1Target:      $('dist1-target'),
    dist2State:       $('dist2-state'),
    dist2Pos:         $('dist2-pos'),
    dist2Max:         $('dist2-max'),
    dist2Blade:       $('dist2-blade'),
    dist2Target:      $('dist2-target'),
    distAction:       $('dist-action'),
    distRoute:        $('dist-route'),
    distributorDiagnostics: $('distributor-diagnostics'),
    controlError:      $('control-error'),

    statsSummary:         $('stats-summary'),
    statsBody:            $('stats-body'),
    statsService:         $('stats-service'),

    historyCards:     $('history-cards'),
    statsPanel:       $('stats-panel'),

    statTotal:        $('stat-total'),
    statGood:         $('stat-good'),
    statBad:          $('stat-bad'),
    statCleanup:      $('stat-cleanup'),
    statInline:       $('stat-inline'),
    statEmpty:        $('stat-empty'),
    lineCells:        $('line-cells'),
    processPhaseLabel: $('process-phase-label'),
    processPhaseDetail: $('process-phase-detail'),
    processPhaseCode: $('process-phase-code'),
    processPhaseStep: $('process-phase-step'),
    processStageTrack: $('process-stage-track'),
    defectsSection:   $('defects-section'),
    defectsTitle:     $('defects-title'),
    defectsList:      $('defects-list'),

    jogPanel:         $('jog-panel'),
    jogLastAction:    $('jog-last-action'),

    jogHwSerial:      $('jog-hw-serial'),
    jogHwCameras:     $('jog-hw-cameras'),
    jogHwConveyor:    $('jog-hw-conveyor'),
    jogHwDist1:       $('jog-hw-dist1'),
    jogHwDist2:       $('jog-hw-dist2'),

    frameAnalysisPanelNew: $('frame-analysis-panel'),
    frameAnalysisPanel: $('frame-analysis-panel'),

    archiveSettingsOpen: $('archive-settings-open'),
    archiveSettingsGroup: $('archive-settings-group'),
    archiveSettingsModal: $('archive-settings-modal'),
    archiveSettingsBackdrop: document.querySelector('.archive-settings-backdrop'),
    archiveSettingsClose: $('archive-settings-close'),
    archiveSettingsCancel: $('archive-settings-cancel'),
    archivePickFolder: $('archive-pick-folder'),
    archiveSettingsSave: $('archive-settings-save'),
    archiveRootPath: $('archive-root-path'),
    archiveJpegQuality: $('archive-jpeg-quality'),
    archiveZipCompression: $('archive-zip-compression'),
    archiveZipLevel: $('archive-zip-level'),
    archiveEnabled: $('archive-enabled'),
    archiveCompressOnShutdown: $('archive-compress-on-shutdown'),
    archiveDeleteOriginal: $('archive-delete-original'),
    archiveSettingsValidation: $('archive-settings-validation'),
    archiveSettingsStatus: $('archive-settings-status'),
    archiveBatchId: $('archive-batch-id'),
    archiveBatchGood: $('archive-batch-good'),
    archiveBatchBad: $('archive-batch-bad'),
    archiveBatchCleanup: $('archive-batch-cleanup'),

    btnStart:         $('btn-start'),
    btnPause:         $('btn-pause'),
    btnResume:        $('btn-resume'),
    btnStop:          $('btn-stop'),
    btnExit:          $('btn-exit'),

    thresholdsPanel:      $('thresholds-panel'),
    thresholdsCameraLabel: $('thresholds-camera-label'),
    thresholdsHint:       $('thresholds-hint'),
    thresholdsBody:       $('thresholds-body'),
    thresholdsStatus:     $('thresholds-status'),
    thresholdsSave:       $('thresholds-save'),
    thresholdsReset:      $('thresholds-reset'),

    galleryModal:     $('gallery-modal'),
    galleryGrid:      $('gallery-grid'),
    galleryPartId:    $('gallery-part-id'),
    galleryCategory:  $('gallery-category'),
    galleryDecision:  $('gallery-decision'),
    galleryTime:      $('gallery-time'),
    galleryBatch:     $('gallery-batch'),
    galleryDefects:   $('gallery-defects-list'),
    galleryClose:     $('gallery-close'),
    galleryModeDebug: $('gallery-mode-debug'),
    galleryModeRaw:   $('gallery-mode-raw'),
};

// ─── Double buffering — защищён от гонок счётчиком —──────────

const mainBuffer = new Image();
let _mainBufferSeq = 0;
let _mainBufferExpectedSeq = 0;
let mainBufferLoading = false;
let mainBufferRequestRole = null;
let mainBufferRequestView = null;
let mainBufferRequestVersion = null;

function showMainCameraFrame(source, kind) {
    if (!els.mainCamera) return;
    // Меняем показанный режим только когда именно этот src будет загружен
    // главным <img>. Последний уже видимый кадр сохраняет честный бейдж.
    state.pendingDisplayKind = kind;
    state.pendingDisplaySeq += 1;
    els.mainCamera.dataset.displaySeq = String(state.pendingDisplaySeq);
    els.mainCamera.src = source;
}

if (els.mainCamera) {
    els.mainCamera.addEventListener('load', () => {
        const seq = Number(els.mainCamera.dataset.displaySeq || 0);
        if (!seq || seq !== state.pendingDisplaySeq) return;
        state.displayedFrameKind = state.pendingDisplayKind;
        state.pendingDisplayKind = null;
        if (typeof applyLiveBadge === 'function') applyLiveBadge(state.jogActive);
    });
}

mainBuffer.addEventListener('load', () => {
    const pullMode = (state.mainCamMode === 'pull' || state.mainCamMode === 'live-pull');
    const mySeq = mainBuffer._seq || 0;
    // Если пришёл не последний запрос — игнорируем (гонка быстрой смены камеры)
    if (mySeq !== _mainBufferExpectedSeq) {
        mainBufferLoading = false;
        if (state.mainCamMode === 'pull' && typeof maybeRequestMainFrame === 'function') {
            setTimeout(maybeRequestMainFrame, 5);
        }
        return;
    }
    const requestIsCurrent = (
        pullMode
        && mainBufferRequestRole === state.currentCamera
        && mainBufferRequestView === state.mode
        && (state.mainCamMode === 'live-pull' || mainBufferRequestVersion === state.currentVersion)
    );
    if (requestIsCurrent) {
        showMainCameraFrame(
            mainBuffer.src,
            state.mainCamMode === 'live-pull' ? 'live' : 'static',
        );
    }
    mainBufferLoading = false;

    if (requestIsCurrent && state.pendingAnalysisVersion !== null && typeof flushPendingAnalysis === 'function') {
        flushPendingAnalysis();
    }

    if (state.mainCamMode === 'live-pull' && typeof scheduleNextLiveFrame === 'function') {
        scheduleNextLiveFrame(requestIsCurrent ? LIVE_CAM_MIN_GAP : 1);
    } else if (state.mainCamMode === 'pull' && !requestIsCurrent && typeof maybeRequestMainFrame === 'function') {
        maybeRequestMainFrame();
    }
});

mainBuffer.addEventListener('error', () => {
    // Если ошибка, но это уже не актуальный запрос — просто отпускаем загрузку
    const mySeq = mainBuffer._seq || 0;
    if (mySeq !== _mainBufferExpectedSeq) {
        mainBufferLoading = false;
        return;
    }
    mainBufferLoading = false;
    if (state.mainCamMode === 'live-pull' && typeof scheduleNextLiveFrame === 'function') {
        scheduleNextLiveFrame(60);
    } else if (state.mainCamMode === 'pull' && typeof maybeRequestMainFrame === 'function') {
        setTimeout(maybeRequestMainFrame, 60);
    }
});

// ─── API ─────────────────────────────────────────────────────

async function api(path, options = {}, controlFeedback = false) {
    try {
        const res = await fetch(path, options);
        const ct = res.headers.get('content-type') || '';
        const payload = ct.includes('json') ? await res.json() : await res.text();
        if (!res.ok) {
            const message = payload && payload.error ? payload.error : `${res.status}`;
            throw new Error(message);
        }
        return payload;
    } catch (err) {
        console.warn(`[API] ${path}:`, err.message);
        if (controlFeedback) showControlError(err.message || `Ошибка запроса ${path}`);
        return null;
    }
}

const apiGet = (path) => api(path);
const apiPost = (path, feedback = false) => api(path, {method: 'POST'}, feedback);
async function apiPostJson(path, payload, feedback = false) {
    return api(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload || {}),
    }, feedback);
}

function showControlError(message) {
    if (!els.controlError) return;
    setIfChanged(els.controlError, message || 'Неизвестная ошибка');
    els.controlError.classList.remove('is-hidden');
}
function clearControlError() {
    if (!els.controlError) return;
    els.controlError.classList.add('is-hidden');
    setIfChanged(els.controlError, '');
}

// ─── Helpers ─────────────────────────────────────────────────

function animateUiElement(el, className = 'ui-value-change') {
    if (!el || !state.uiRevealed) return;
    el.classList.remove(className);
    void el.offsetWidth;
    el.classList.add(className);
    setTimeout(() => el.classList.remove(className), 260);
}
function normalizeOperatorText(value) { return String(value).replace(/\u2116\s*/g, '#'); }
function setIfChanged(el, value) {
    if (!el) return;
    const text = normalizeOperatorText(value);
    if (el.textContent === text) return;
    el.textContent = text;
    if (el.classList.contains('stats-value') || el.classList.contains('axis-state') || el.classList.contains('state-label')) {
        animateUiElement(el);
    }
}
function cameraRoleLabel(role) { return CAMERA_ROLE_LABELS[role] || role || '—'; }
function lineStateLabel(value) { return LINE_STATE_LABELS[String(value || '').toUpperCase()] || value || '—'; }
function axisStateLabel(value) { return AXIS_STATE_LABELS[String(value || '').toUpperCase()] || value || '—'; }
function categoryLabel(value) { return CATEGORY_LABELS[String(value || '').toUpperCase()] || value || '—'; }
function formatFrameRate(value) { const n = Number(value || 0).toFixed(1).replace('.', ','); return `${n} КАДР/С`; }
function distributorTargetLabel(value) { if (!value || value === '-') return '—'; return categoryLabel(value); }
function distributorActionLabel(value) {
    if (!value || value === '-') return '—';
    return String(value)
        .replace('HOMED', 'ОСИ В НУЛЕ')
        .replace('PARK FOR PRODUCTION', 'ПОДГОТОВКА К РАБОТЕ')
        .replace('PRODUCTION READY', 'ГОТОВО К РАБОТЕ')
        .replace('DIAGNOSTIC', 'ПРОВЕРКА')
        .replace('DIST1 -> HOME', 'DIST1 -> ГОДНО')
        .replace('DIST1 -> OPEN', 'DIST1 -> НА DIST2')
        .replace('DIST2 -> BAD', 'DIST2 -> БРАК')
        .replace('DIST2 -> CLEANUP', 'DIST2 -> ОЧИСТКА')
        .replace('DIST1_HOME', 'DIST1 ПРОХОД')
        .replace('DIST1_OPEN', 'DIST1 СБРОС')
        .replace('DIST2_BAD', 'DIST2 БРАК')
        .replace('DIST2_CLEANUP', 'DIST2 ОЧИСТКА')
        .replace(/PART #(\d+)/g, 'ДЕТАЛЬ #$1')
        .replace('PART', 'ДЕТАЛЬ')
        .replace('DROP...', 'СБРОС...')
        .replace('PASS', 'ПРОХОД')
        .replace('DONE', 'ГОТОВО')
        .replace('BAD', 'БРАК')
        .replace('CLEANUP', 'ОЧИСТКА')
        .replace('EMERGENCY STOP', 'АВАРИЙНАЯ ОСТАНОВКА');
}
function jogActionLabel(value) {
    if (!value || value === '-') return '—';
    const text = String(value);
    if (text === 'HOLD RIGHT') return 'ДВИЖЕНИЕ ВПРАВО';
    if (text === 'HOLD LEFT') return 'ДВИЖЕНИЕ ВЛЕВО';
    if (text.startsWith('ERR:')) return `ОШИБКА:${text.slice(4)}`;
    if (text.startsWith('STOP:')) {
        if (text.includes('heartbeat timeout')) return 'ОСТАНОВЛЕНО: ПОТЕРЯ СИГНАЛА УДЕРЖАНИЯ';
        return 'ОСТАНОВЛЕНО';
    }
    return text.replace('UI ONLY', 'ДЕМО-РЕЖИМ');
}

// Немедленный статус — отменяет запланированный тик и делает внеплановый запрос
let _statusLoopTimer = null;
let _statusImmediatePending = false;

function requestImmediateStatus() {
    if (_statusImmediatePending) return;
    _statusImmediatePending = true;
    if (_statusLoopTimer) { clearTimeout(_statusLoopTimer); _statusLoopTimer = null; }
    setTimeout(async () => {
        _statusImmediatePending = false;
        await fetchStatus();
        scheduleNextStatusTick();
    }, 10);
}

function scheduleNextStatusTick() {
    if (_statusLoopTimer) clearTimeout(_statusLoopTimer);
    const next = getStatusInterval();
    _statusLoopTimer = setTimeout(async () => {
        _statusLoopTimer = null;
        await fetchStatus();
        scheduleNextStatusTick();
    }, next);
    state.statusInterval = _statusLoopTimer;
}

async function sendActiveCameraIfChanged(role) {
    if (!role || state.offline) return;
    if (state.lastSentActiveCamera === role && !state.activeCameraRequestBusy) return;
    state.pendingActiveCamera = role;
    if (state.activeCameraRequestBusy) return;

    state.activeCameraRequestBusy = true;
    try {
        while (state.pendingActiveCamera) {
            const target = state.pendingActiveCamera;
            state.pendingActiveCamera = null;
            const result = await apiPost(`/api/active_camera/${encodeURIComponent(target)}`);
            if (result) state.lastSentActiveCamera = target;
            else state.lastSentActiveCamera = null;
        }
    } finally {
        state.activeCameraRequestBusy = false;
    }
    if (!state.offline && state.currentCamera && state.lastSentActiveCamera !== state.currentCamera) {
        setTimeout(() => sendActiveCameraIfChanged(state.currentCamera), 400);
    }
}

// ─── Adaptive polling ────────────────────────────────────────

function getStatusInterval() {
    const s = state.lineState;
    if (state.controlPending || state.distributorDiagnosticPending || state.distributorDiagnosticBackendBusy) return STATUS_INTERVAL_MOTION;
    if (s === 'RUNNING' || s === 'PAUSED' || s === 'STOPPING') return STATUS_INTERVAL_FAST;
    if (state.serverExitRequested) return STATUS_INTERVAL_FAST;
    if (state.jogActive) return STATUS_INTERVAL_FAST;
    return STATUS_INTERVAL_IDLE;
}

function startStatusPolling() {
    if (_statusLoopTimer) clearTimeout(_statusLoopTimer);
    if (state.statusInterval) {
        try { clearInterval(state.statusInterval); } catch (_) {}
        try { clearTimeout(state.statusInterval); } catch (_) {}
    }
    scheduleNextStatusTick();
}

function stopStatusPolling() {
    if (_statusLoopTimer) { clearTimeout(_statusLoopTimer); _statusLoopTimer = null; }
    if (state.statusInterval) {
        try { clearInterval(state.statusInterval); } catch (_) {}
        try { clearTimeout(state.statusInterval); } catch (_) {}
        state.statusInterval = null;
    }
}
