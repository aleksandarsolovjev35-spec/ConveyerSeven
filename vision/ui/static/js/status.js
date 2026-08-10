// status.js — Line Monitor UI module (stabilized)
// - защита от гонок в fetchStatus через очередность и версионирование
// - стабильный polling через core.js (setTimeout loop)
// - синхронизация монитор->UI сохранена
'use strict';

function updateOperationalAccordions(lineState) {
    const fullyStopped = lineState === 'IDLE' || lineState === 'STOPPED';
    if (els.statsBody) els.statsBody.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-open');
    if (els.statsService) els.statsService.classList.remove('is-collapsed');
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.toggle('controls-collapsed', !fullyStopped);
        els.distributorDiagnostics.querySelectorAll('.blade-diagnostic-grid').forEach(grid => {
            grid.classList.toggle('is-collapsed', !fullyStopped);
        });
    }
}

function setBladeMarkerPosition(marker, percent) {
    if (!marker) return;
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    marker.style.left = `${normalized}%`;
}

// Очередность запросов статуса — избегаем наложения
let _statusSeq = 0;
let _lastHandledStatusSeq = 0;

async function fetchStatus() {
    if (!state.bootDone) return;
    if (state.statusFetchBusy) {
        // Если уже идёт запрос, пропускаем тик — следующий тик возьмёт свежие данные
        return;
    }
    const mySeq = ++_statusSeq;
    state.statusFetchBusy = true;

    const status = await apiGet('/api/status');
    state.statusFetchBusy = false;

    if (!status) {
        const reference = state.lastStatusAt || state.bootDoneAt;
        if (reference > 0 && Date.now() - reference >= STATUS_OFFLINE_AFTER) markUiOffline();
        return;
    }

    // If a reconnect/poll delivers an older logical snapshot, ignore it.
    // Frame versions are independent and must not be used as state ordering.
    if (mySeq < _lastHandledStatusSeq) return;
    if (typeof status.state_version === 'number'
        && status.state_version < state.lastStateVersion) return;
    _lastHandledStatusSeq = mySeq;
    if (typeof status.state_version === 'number') {
        state.lastStateVersion = status.state_version;
    }

    state.lastStatusAt = Date.now();
    state.statusReceived = true;
    if (state.offline) {
        state.offline = false;
        els.main.classList.remove('ui-offline');
        clearControlError();
    }

    if (status.frame_versions && typeof status.frame_versions === 'object') {
        state.frameVersions = {...status.frame_versions};
    }

    if (typeof status.thresholds_revision === 'number') {
        if (state.thresholdsRevision !== null && status.thresholds_revision !== state.thresholdsRevision && typeof updateThresholdsPanel === 'function') {
            updateThresholdsPanel(true);
        }
        state.thresholdsRevision = status.thresholds_revision;
    }

    const incomingVersion = typeof status.frame_version === 'number' ? status.frame_version : null;
    const newPublishArrived = incomingVersion !== null && incomingVersion !== state.lastSeenVersion;

    if (incomingVersion !== null) {
        state.currentVersion = incomingVersion;
        if (newPublishArrived && state.mainCamMode === 'pull' && !state.splashActive) {
            maybeRequestMainFrame();
        }
    }

    if (typeof updateArchiveStatus === 'function') {
        updateArchiveStatus(status.archive || null);
    }

    const lineStatusPayload = status.line_status || {};
    if (els.simulationBadge) {
        els.simulationBadge.classList.toggle('is-hidden', lineStatusPayload.simulation !== true);
    }
    const liveInfo = lineStatusPayload.live || {};
    const staticRoles = Array.isArray(liveInfo.static_roles) ? liveInfo.static_roles : [];
    const processInfo = lineStatusPayload.process || {};
    const inspectionRoles = Array.isArray(processInfo.inspection_roles) ? processInfo.inspection_roles : [];
    const processPhase = String(processInfo.phase || '').toUpperCase();
    const inspectionDisplay = liveInfo.static === true
        && inspectionRoles.includes(state.currentCamera)
        && (processPhase.includes('CAMERA') || processPhase.includes('ANALYSIS') || processPhase === 'PUBLISH');
    const selectedRoleStatic = liveInfo.all_roles_static === true
        || staticRoles.includes(state.currentCamera)
        || inspectionDisplay
        // Совместимость со статусом backend до ролевых пауз.
        || (liveInfo.static === true && liveInfo.streaming === false && staticRoles.length === 0);
    const staticPublish = (
        newPublishArrived
        && incomingVersion > 0
        && !state.splashActive
        && selectedRoleStatic
        && state.mainCamMode === 'pull'
    );
    if (staticPublish) {
        state.pendingAnalysisVersion = incomingVersion;
        armPendingFlushFallback();
    }
    state.lastLineStatus = lineStatusPayload;

    const oldState = state.lineState;
    updateLineStatus(lineStatusPayload);
    updateRecentParts(status.recent_parts || []);
    updateMode(status.mode || 'RULES');

    if (state.lineState !== oldState) {
        startStatusPolling();
    }

    checkUiReady();
}

function markUiOffline() {
    if (state.offline) return;
    state.offline = true;
    state.controlPending = false;
    state.startPending = false;
    state.jogTogglePending = false;
    state.distributorDiagnosticPending = false;
    state.jogActive = false;
    state.jogBusy = false;
    if (typeof clearLivePullTimer === 'function') clearLivePullTimer();
    state.mainCamMode = 'pull';
    mainBufferLoading = false;
    els.main.classList.add('ui-offline');
    updateProcessPhaseLabel('OFFLINE');
    showControlError('Нет связи с backend. Все команды заблокированы.');
    releaseJogHoldBestEffort('backend offline');
    applyButtonsForState('OFFLINE', true, {});
    updateDistributorDiagnosticControls({diagnostic_allowed: false, diagnostic_busy: true});
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
    updateViewModeControls();
    if (els.jogPanel) {
        els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(button => { button.disabled = true; });
    }
    if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
    if (typeof updateArchiveStatus === 'function') updateArchiveStatus(null);
    updateStateOverlay({state: 'OFFLINE', in_line: 0});
}

function updateProcessPhaseLabel(lineState, phase) {
    const phaseEl = els.processPhaseLabel || document.getElementById('process-phase-label');
    if (!phaseEl) return;
    const activeState = String(lineState || 'IDLE').toUpperCase();
    let label = lineStateLabel(activeState);
    // Детальная фаза шага: например «РАБОТАЕТ · ЛЕНТА ДВИЖЕТСЯ». Для
    // состояний останова/аварии/оффлайн фазу не добавляем — там достаточно
    // операторского названия состояния.
    if ((activeState === 'RUNNING' || activeState === 'STOPPING'
         || activeState === 'PAUSED')
        && typeof processPhaseLabel === 'function') {
        const detail = processPhaseLabel(phase);
        if (detail) label += ' · ' + detail;
    }
    setIfChanged(phaseEl, label);
    phaseEl.dataset.lineState = activeState;
    phaseEl.style.opacity = '1';
    if (activeState === 'RUNNING') phaseEl.style.color = 'var(--ok)';
    else if (activeState === 'PAUSED' || activeState === 'STOPPING') phaseEl.style.color = 'var(--warn)';
    else if (activeState === 'FAULT' || activeState === 'OFFLINE') phaseEl.style.color = 'var(--bad)';
    else phaseEl.style.color = 'var(--text-dim)';
}

function updateLineStatus(ls) {
    const lineState = ls.state || 'IDLE';
    const exitRequested = !!ls.exit_requested;

    state.lineState = lineState;
    state.serverExitRequested = exitRequested;
    if (!['IDLE', 'STOPPED'].includes(lineState)) state.startPending = false;
    updateOperationalAccordions(lineState);

    const process = ls.process || {};
    updateProcessPhaseLabel(lineState, process.phase);
    setIfChanged(els.metricStep, ls.step || 0);

    state.backendControls = ls.controls || {};
    applyButtonsForState(lineState, exitRequested, state.backendControls);
    updateViewModeControls();

    setIfChanged(els.statTotal, ls.total || 0);
    setIfChanged(els.statGood, ls.good || 0);
    setIfChanged(els.statBad, ls.rejected || 0);
    setIfChanged(els.statCleanup, ls.cleanup || 0);
    setIfChanged(els.statEmpty, ls.empty || 0);

    const pendingAnalysis = state.pendingAnalysisVersion !== null;
    const inLine = pendingAnalysis ? _appliedInLine : (ls.in_line || 0);
    setIfChanged(els.statInline, `${inLine} / 8`);

    _updateDistributorRoute(ls);
    updateLineCells(ls.line_parts || [], process);
    updateDistributorDiagnosticControls(ls);
    updateSelectedAnalysisStatus(ls);
    if (!pendingAnalysis && typeof updateNewFrameAnalysisStatus === 'function') {
        updateNewFrameAnalysisStatus(ls);
    }

    const d1State = ls.dist1_state || 'IDLE';
    if (els.dist1State) els.dist1State.className = `axis-state axis-${d1State.toLowerCase()}`;
    setIfChanged(els.dist1State, axisStateLabel(d1State));
    const d1Pos = Math.max(0, Number(ls.dist1_position || 0));
    const d1Max = Math.max(1, Number(ls.dist1_max || 340));
    setIfChanged(els.dist1Pos, d1Pos);
    setIfChanged(els.dist1Max, d1Max);
    if (els.dist1Blade) {
        const d1Percent = Math.max(0, Math.min(100, d1Pos / d1Max * 100));
        setBladeMarkerPosition(els.dist1Blade, d1Percent);
    }
    const d1Moving = ['MOVING', 'MOVING_TO_GOOD', 'MOVING_TO_DIST2', 'HOMING'].includes(String(d1State).toUpperCase());
    const d1TargetLabel = d1Moving ? 'ПЕРЕМЕЩЕНИЕ' : (d1Pos <= 0 ? 'ГОДНО' : (d1Pos >= d1Max ? 'НА DIST2' : `ПОЗИЦИЯ ${d1Pos}`));
    setIfChanged(els.dist1Target, d1TargetLabel);

    const d2State = ls.dist2_state || 'IDLE';
    if (els.dist2State) els.dist2State.className = `axis-state axis-${d2State.toLowerCase()}`;
    setIfChanged(els.dist2State, axisStateLabel(d2State));
    const d2Pos = Math.max(0, Number(ls.dist2_position || 0));
    const d2Max = Math.max(1, Number(ls.dist2_max || 340));
    setIfChanged(els.dist2Pos, d2Pos);
    setIfChanged(els.dist2Max, d2Max);
    setIfChanged(els.dist2Target, distributorTargetLabel(ls.dist2_target));
    if (els.dist2Blade) {
        const d2Percent = Math.max(0, Math.min(100, d2Pos / d2Max * 100));
        setBladeMarkerPosition(els.dist2Blade, d2Percent);
    }

    setIfChanged(els.distAction, distributorActionLabel(ls.last_distributor_action));

    updateJogState(ls.jog || null);
    updateStateOverlay(ls);
    updateJogHardware(ls);
    handleJogAutoToggle(lineState, ls.jog || null);

    if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
    if (typeof updateArchiveButton === 'function') updateArchiveButton();
}

// ─── Line cells ──────────────────────────────────────────────
const _lineTokens = new Map();
// Tokens that are fading after leaving the logical line. They are kept out
// of _lineTokens so a new status cannot move them, but a fast consecutive drop
// must still be able to remove an old chute token before adding another one.
const _lineExitTokens = new Set();
let _lineSyncDone = false;
let _appliedLineParts = [];
let _appliedInLine = 0;
// Длительность анимации падения в лоток (token-sink). После горизонтального
// скольжения до +8 элемент падает вниз ровно этот промежуток времени.
const CHUTE_SINK_MS = 700;

function lineMoveDuration(process = {}) {
    const conv = process.conveyor || {};
    const speed = Number(conv.speed) || 0;
    if (!speed) return 700;
    return Math.max(400, Math.min(1100, Math.round(8400000 / speed * 1.7)));
}

// Корпус (человек) стоит в ОКНЕ вагона. Окно = ВСЯ ячейка (WINDOW_PAD 0),
// чтобы непрозрачная стенка ленты НЕ перекрывала 1px-рамку позиции (иначе
// обводка «срезается»). Стенки остаются только в зазорах между ячейками
// (3px), за которыми корпус скрывается при переезде. Сам корпус чуть меньше
// окна (TOKEN_SCALE), поэтому не вылезает за рамку.
const WINDOW_PAD = 0;
const TOKEN_SCALE_W = 0.96;
const TOKEN_SCALE_H = 0.92;

// Окно вагона = ячейка, сжатая на WINDOW_PAD с каждой стороны. Именно эти
// прямоугольники пробиваются в маске тела вагона (и в них стоит человек).
function _windowRect(r) {
    return {
        left: r.left + WINDOW_PAD,
        top: r.top + WINDOW_PAD,
        width: r.width - 2 * WINDOW_PAD,
        height: r.height - 2 * WINDOW_PAD,
    };
}

function tokenBox(target) {
    // Центрируем корпус и СНАПим к целым пикселям синхронно по центру.
    // Если оставить дробный left/width, 1px рамка корпуса растеризуется с
    // антиалиасингом и «смазывается», из-за чего корпус визуально съезжает
    // то влево, то вправо на разных позициях. Округляем центр к целому, а
    // отступ строим от округлённой ширины — тогда обе границы ложатся на
    // целые пиксели и рамка рисуется чётко.
    const w = Math.max(8, Math.round(target.width * TOKEN_SCALE_W));
    const h = Math.max(6, Math.round(target.height * TOKEN_SCALE_H));
    // Центр ячейки (дробный), округлённый к ближайшему целому.
    const cx = Math.round(target.left + target.width / 2);
    const cy = Math.round(target.top + target.height / 2);
    return {
        left: cx - Math.round(w / 2),
        top: cy - Math.round(h / 2),
        width: w,
        height: h,
    };
}

// ``CONVEYOR_CONFIRMED`` is published after the controller has already
// advanced the logical positions.  Treating every phase containing the word
// CONVEYOR as motion advances the same token for a second time, then makes it
// jump back on SETTLE.  Keep the transport phases explicit so the animation
// has exactly one step per physical move.
function isConveyorTransportPhase(phase) {
    const p = String(phase || '').toUpperCase();
    return p === 'CONVEYOR'
        || p === 'CONVEYOR_COMMAND'
        || p === 'CONVEYOR_MOVING'
        || p === 'MOTION'
        || p.startsWith('MOTION_');
}

function _lineCellRects(cells) {
    if (!els.lineCells) return {containerRect: {width: 0}, rects: {}};
    const containerRect = els.lineCells.getBoundingClientRect();
    const rects = {};
    cells.forEach(cell => {
        const r = cell.getBoundingClientRect();
        // НЕ округляем координаты: независимое округление каждой ячейки
        // ломает общие границы между соседями (сумма не сходится), из-за чего
        // маска-тело вагона перекрывает обводку позиции то с одной, то с
        // другой стороны. Держим дробные координаты — и маска, и токены
        // используют одну и ту же геометрию, совпадающую с реальными границами.
        rects[Number(cell.dataset.pos)] = {
            left: r.left - containerRect.left,
            top: r.top - containerRect.top,
            width: r.width,
            height: r.height,
        };
    });
    return {containerRect, rects};
}

// Строит SVG-маску «окон вагона» в ОТНОСИТЕЛЬНЫХ координатах (0..100),
// как и сама сетка из колонок 1fr. viewBox + mask-size 100% заставляют маску
// масштабироваться вместе с контейнером так же, как сетку, — поэтому окна
// всегда совпадают с границами позиций независимо от ширины и масштаба.
// Маска непрозрачна (тело вагона) везде, КРОМЕ прямоугольников-окон по
// Строит SVG-маску «окон вагона» в АБСОЛЮТНЫХ пикселях (width/height равны
// реальному размеру панели), координаты окон — в px. mask-size {W}px {H}px
// кладёт её 1:1. Это рабочий вариант, при котором окна совпадали с ячейками
// и анимации шли внутри вагона (без viewBox/относительных координат, которые
// искажали пропорции окон на контроле).
function _buildWindowMaskSvg(rects, W, H) {
    // Маска в АБСОЛЮТНЫХ пикселях (width=W, height=H), как в рабочем вагоне.
    // SVG viewBox="0 0 100 100" квадратный, а панель широкая — при
    // mask-size 100% 100% браузер мог сохранять пропорции и «сжимать» маску.
    // Абсолютные px дают правильную форму (W×H) и mask-size в px кладёт 1:1.
    let d = `M0,0 L${W},0 L${W},${H} L0,${H} Z `;
    // Окна «вагона» — ячейки, сжатые на WINDOW_PAD (та же геометрия, что у
    // токена): вокруг каждого окна остаётся непрозрачная стенка вагона, из-за
    // которой человек исчезает при переезде между окнами.
    for (let pos = 0; pos <= 8; pos++) {
        const r = rects[pos];
        if (!r) continue;
        const w = _windowRect(r);
        const x0 = w.left, y0 = w.top;
        const x1 = w.left + w.width, y1 = w.top + w.height;
        if (x1 <= x0 || y1 <= y0) continue;
        d += `M${x0},${y0} L${x1},${y0} L${x1},${y1} L${x0},${y1} Z `;
    }
    const svg =
        `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}">` +
        `<path fill="black" fill-rule="evenodd" d="${d}"/></svg>`;
    return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

function _applyWindowMask(containerRect, rects) {
    const belt = els.lineCells && els.lineCells.querySelector('.conveyor-belt');
    if (!belt) return;
    // Абсолютная маска в px, mask-size = фактический размер ленты (дробный),
    // чтобы маска ложилась 1:1 с панелью без субпиксельного зазора.
    const W = containerRect.width;
    const H = containerRect.height;
    const uri = _buildWindowMaskSvg(rects, W, H);
    belt.style.webkitMaskImage = uri;
    belt.style.maskImage = uri;
    belt.style.webkitMaskSize = `${W}px ${H}px`;
    belt.style.maskSize = `${W}px ${H}px`;
    belt.style.webkitMaskRepeat = 'no-repeat';
    belt.style.maskRepeat = 'no-repeat';
}

function _applyTokenCategory(el, category) {
    el.classList.remove('cell-good', 'cell-bad', 'cell-cleanup');
    if (category === 'BAD') el.classList.add('cell-bad');
    else if (category === 'CLEANUP') el.classList.add('cell-cleanup');
    else if (category === 'GOOD') el.classList.add('cell-good');
}

function _removeLineTokenElement(token) {
    if (!token) return;
    token.parkedPass = false;
    _lineExitTokens.delete(token);
    if (token.el && token.el.parentNode) token.el.parentNode.removeChild(token.el);
}

function _scheduleLineTokenRemoval(token, cells, delayMs) {
    setTimeout(() => {
        _removeLineTokenElement(token);
        _updateChuteOccupied(cells);
    }, delayMs);
}

function _animateLineTokenExit(token, cells, leftPx, removalDelayMs) {
    token.el.style.left = `${leftPx}px`;
    token.el.style.opacity = '0';
    _scheduleLineTokenRemoval(token, cells, removalDelayMs);
}

function _clearChuteExitTokens() {
    for (const token of [..._lineExitTokens]) {
        if (token.exitPosition === 8) _removeLineTokenElement(token);
    }
}

function _updateChuteOccupied(cells) {
    const occupied = [..._lineTokens.values()].some(token => token.position === 8)
        || [..._lineExitTokens].some(token => (
            token.exitPosition === 8
            && token.el
            && token.el.parentNode
        ));
    cells.forEach(cell => {
        if (Number(cell.dataset.pos) === 8) {
            cell.classList.toggle('chute-occupied', occupied);
        }
    });
}

const ROUTE_CATEGORIES = ['GOOD', 'BAD', 'CLEANUP'];
let _currentDistributorCategory = '';

function _resolveDistributorRoute(ls) {
    const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
    const process = ls.process || {};
    const phaseText = String(process.phase || '').toUpperCase();
    const routingPhase = phaseText.includes('ROUTE') || phaseText.includes('DROP');
    let part = null;
    if (routingPhase && process.part_id != null) {
        part = parts.find(item => Number(item.id) === Number(process.part_id)) || null;
    }
    if (!part) part = parts.find(item => Number(item.position) === 7) || null;
    let category = part ? String(part.category || '').toUpperCase() : '';
    if (!ROUTE_CATEGORIES.includes(category)) category = '';
    if (!category) {
        const d1State = String(ls.dist1_state || '').toUpperCase();
        const d1ToDist2 = ['TO_DIST2', 'MOVING_TO_DIST2'].includes(d1State) || (d1State !== 'MOVING_TO_GOOD' && Number(ls.dist1_position || 0) > 0);
        if (d1ToDist2) category = String(ls.dist2_target || '').toUpperCase() === 'CLEANUP' ? 'CLEANUP' : 'BAD';
    }
    return category;
}

function _updateDistributorRoute(ls) {
    const panel = els.distributorDiagnostics;
    if (!panel) return;
    panel.classList.remove('route-good', 'route-bad', 'route-cleanup', 'production-ready');
    const category = _resolveDistributorRoute(ls);
    let effective = '';
    if (category === 'GOOD') { panel.classList.add('route-good'); effective = 'GOOD'; }
    else if (category === 'BAD') { panel.classList.add('route-bad'); effective = 'BAD'; }
    else if (category === 'CLEANUP') { panel.classList.add('route-cleanup'); effective = 'CLEANUP'; }
    else {
        const lineState = (ls.state || state.lineState || '').toUpperCase();
        const d1State = String(ls.dist1_state || '').toUpperCase();
        const movingToGood = d1State === 'MOVING_TO_GOOD';
        const parked = ['IDLE', 'STOPPED'].includes(lineState) && (d1State === 'GOOD' || movingToGood) && (Number(ls.dist1_position || 0) === 0 || movingToGood);
        if (parked) { panel.classList.add('production-ready'); effective = 'GOOD'; }
    }
    _currentDistributorCategory = effective;
    if (els.distRoute) {
        const ready = panel.classList.contains('production-ready');
        const label = category ? `→ ${categoryLabel(category)}` : (ready ? 'ПРОИЗВОДСТВО ГОТОВО' : '');
        setIfChanged(els.distRoute, label);
    }
}

function updateLineCells(lineParts, process = {}) {
    if (!els.lineCells) return;
    const phase = String(process.phase || '').toUpperCase();
    let belt = els.lineCells.querySelector('.conveyor-belt');
    if (!belt) {
        belt = document.createElement('div');
        belt.className = 'conveyor-belt';
        els.lineCells.insertBefore(belt, els.lineCells.firstChild);
    }
    // Подсветка ленты во время движения убрана: лента всегда статична,
    // класс «moving» не применяется (в CSS для него больше нет стилей).
    els.lineCells.style.setProperty('--move-duration', `${lineMoveDuration(process)}ms`);

    const cells = els.lineCells.querySelectorAll('.line-cell[data-pos]');
    const active = Array.isArray(process.positions) ? process.positions : [];
    const phaseUpper = phase;
    // Подсветка ячеек зелёным во время движения ленты убрана: в транспортных
    // фазах (CONVEYOR_MOVING/MOTION) не вешаем process-active (зелёная рамка).
    const isTransportPhase = isConveyorTransportPhase(phase);
    cells.forEach(cell => {
        const position = Number(cell.dataset.pos);
        cell.className = 'line-cell';
        if (position === 8) cell.classList.add('line-cell-chute');
        if (active.includes(position) && !isTransportPhase) {
            cell.classList.add('process-active');
            if (phase.includes('CAMERA') || phase.includes('ANALYSIS')) cell.classList.add('process-camera');
            if (phase.includes('ROUTE') || phase.includes('DROP')) cell.classList.add('process-route');
        }
    });

    // Корпус на +7 ожидает следующего движения ленты; механического
    // удержания нет. Лоток +8 показывает подготовленный маршрут BAD/CLEANUP.
    // Поле held сохранено только ради обратной совместимости старых снимков
    // статуса и новым backend больше не выставляется.
    const sortPart = (lineParts || []).find(p => Number(p.position) === 7);
    const sortHeld = !!(sortPart && sortPart.held);
    const sortCat = sortPart ? String(sortPart.category || '').toUpperCase() : '';
    let chuteCat = sortCat;
    if (!sortPart) {
        // Пауза серии с пустой ячейкой: лоток держит канал распределителя,
        // чтобы оператор видел, куда пойдёт следующий корпус той же категории.
        if (_currentDistributorCategory === 'GOOD') chuteCat = 'GOOD';
        else if (_currentDistributorCategory === 'BAD') chuteCat = 'BAD';
        else if (_currentDistributorCategory === 'CLEANUP') chuteCat = 'CLEANUP';
    }
    cells.forEach(cell => {
        const position = Number(cell.dataset.pos);
        if (position === 7 && sortHeld) cell.classList.add('cell-hold');
        if (position === 8) {
            // Лоток подсвечивается цветом маршрута корпуса: BAD — красный,
            // CLEANUP — жёлтый, GOOD — зелёный (тот же принцип, что у
            // красного и жёлтого — сброс загорается цветом категории).
            if (chuteCat === 'BAD') cell.classList.add('chute-bad');
            else if (chuteCat === 'CLEANUP') cell.classList.add('chute-cleanup');
            else if (chuteCat === 'GOOD') cell.classList.add('chute-good');
        }
    });

    const pendingAnalysis = state.pendingAnalysisVersion !== null;
    const appliedById = new Map(_appliedLineParts.map(part => [part.id, part.category]));
    const wanted = new Map();
    for (const part of lineParts || []) {
        const id = Number(part.id);
        if (pendingAnalysis && !appliedById.has(id)) continue;
        let category = (part.category || '').toUpperCase();
        if (pendingAnalysis && appliedById.has(id)) category = appliedById.get(id);
        wanted.set(id, {
            position: Math.max(0, Math.min(Number(part.position) || 0, 7)),
            category,
            held: !!(part && part.held),
            dropping: !!(part && part.dropping),
        });
    }

    const {containerRect, rects} = _lineCellRects(cells);
    const geometryReady = !!(containerRect.width && rects[0] && rects[0].width);
    const step = (rects[0] && rects[1]) ? (rects[1].left - rects[0].left) : (rects[0] ? rects[0].width + 3 : 0);
    const duration = lineMoveDuration(process);
    const isConveyorPhase = isConveyorTransportPhase(phaseUpper);
    const isDropPhase = phaseUpper === 'PART_DROP'
        || phaseUpper === 'CONVEYOR_CONFIRMED';

    // Припаркованные годные ждут движения ленты: backend снимает годный
    // корпус с учёта ещё на ROUTE_CHECK (лента стоит), но физически он
    // падает с +7 только со следующим шагом движения. Годный падает в
    // статику — в той же зоне +8, что и реджект, только без цвета канала:
    // маркер гаснет в +8, не «выезжая» за ворота.
    if (isConveyorPhase && geometryReady) {
        for (const token of [..._lineExitTokens]) {
            if (!token.parkedPass) continue;
            token.parkedPass = false;
            const exitLeft = rects[8]
                ? rects[8].left
                : (rects[token.position] ? rects[token.position].left + step : step);
            if (rects[8]) token.exitPosition = 8;
            _animateLineTokenExit(token, cells, exitLeft, duration + 80);
        }
    }

    for (const [id, token] of [..._lineTokens.entries()]) {
        if (wanted.has(id)) continue;
        _lineTokens.delete(id);
        token.exitPosition = token.dropping ? 8 : null;
        _lineExitTokens.add(token);
        if (token.dropping) {
            // Падение: маркер доехал до лотка +8 и «ныряет» вниз через CSS
            // (token-in-chute); номер остаётся видимым. Плашку не гасим
            // inline-opacity, чтобы число не пропало вместе с ней. Удаляем
            // только после того, как горизонтальное скольжение (duration) и
            // само падение (CHUTE_SINK_MS) полностью отработали.
            _scheduleLineTokenRemoval(
                token, cells, duration + CHUTE_SINK_MS + 120
            );
            continue;
        }
        if (geometryReady && rects[token.position]) {
            // Съезд с линии или проход годного: маркер уезжает за ячейку.
            // Годный с +7 при DIST1 на концевике тоже падает — в статику,
            // в той же зоне +8, что и реджект, только без цвета канала:
            // маркер гаснет в +8, не «выезжая» за ворота.
            const passingGood = token.position === 7 && token.category === 'GOOD' && rects[8];
            if (passingGood && !isConveyorPhase) {
                // Статичная фаза: лента стоит, корпус физически ещё на +7.
                // Паркуем маркер на месте до транспортной фазы (см. триггер
                // выше); если движения долго нет (линия остановилась) —
                // гасим на месте запасным таймером.
                token.parkedPass = true;
                setTimeout(() => {
                    if (!token.parkedPass) return;
                    token.parkedPass = false;
                    token.el.style.opacity = '0';
                    _scheduleLineTokenRemoval(token, cells, duration + 80);
                }, Math.max(1600, duration * 4));
                continue;
            }
            if (passingGood) token.exitPosition = 8;
            const exitLeft = passingGood
                ? rects[8].left
                : rects[token.position].left + step;
            _animateLineTokenExit(token, cells, exitLeft, duration + 80);
        } else {
            _scheduleLineTokenRemoval(token, cells, duration + 80);
        }
    }

    if (!geometryReady) {
        for (const [id, meta] of wanted) {
            const token = _lineTokens.get(id);
            if (token) token.position = meta.position;
        }
        _updateChuteOccupied(cells);
        return;
    }

    // Окна «вагона» подгоняем под фактические границы ячеек: маска тела
    // ленты строится по измеренным rect, поэтому окна всегда совпадают с
    // позициями, и видны их рамки.
    _applyWindowMask(containerRect, rects);

    for (const [id, meta] of wanted) {
        let token = _lineTokens.get(id);

        // Куда физически уезжает маркер в этом снимке. Дроп происходит,
        // когда лента несёт корпус от +7 к лотку +8: во время фазы
        // CONVEYOR маркер скользит в зону сброса и остаётся там до
        // исчезновения детали. В остальных случаях лента везёт все корпуса
        // синхронно — во время CONVEYOR каждый маркер скользит на ячейку вправо.
        let targetPos = meta.position;
        let dropAnimationActive = false;
        if (meta.dropping) {
            const alreadyInChute = !!(token && token.position === 8);
            dropAnimationActive = alreadyInChute || isConveyorPhase || isDropPhase;
            if (dropAnimationActive) targetPos = 8;
            // ``CONVEYOR_CONFIRMED`` is not a second belt move, but it is a
            // safe point at which a late first render must still show the
            // pending body in the chute.  This avoids leaving it at +7 when
            // the browser missed the short CONVEYOR_MOVING snapshot.
        } else if (isConveyorPhase) {
            targetPos = Math.min(meta.position + 1, 7);
        }

        if (targetPos === 8) _clearChuteExitTokens();
        const target = rects[targetPos] || rects[meta.position] || rects[0];
        // Маркер в лотке «ныряет» вниз через CSS-анимацию token-sink; плашка
        // остаётся видимой (opacity 1), чтобы номер корпуса читался весь сброс.
        // Никакой inline-opacity:0 — иначе число спрячется вместе с плашкой.
        const targetOpacity = '1';
        // Человек стоит в ОКНЕ вагона (окно = ячейка, сжатая на WINDOW_PAD),
        // той же геометрии, что и прозрачная часть маски. Так человек центрирован
        // в окне и при переезде скрывается за непрозрачной стенкой вагона.
        const box = tokenBox(_windowRect(target));
        if (!token) {
            const el = document.createElement('div');
            el.className = 'line-token';
            el.dataset.partId = String(id);
            el.style.top = `${box.top}px`;
            el.style.width = `${box.width}px`;
            el.style.height = `${box.height}px`;
            token = {el, position: targetPos, category: null, dropping: false, held: false};
            _lineTokens.set(id, token);
            els.lineCells.appendChild(el);
            // Номер корпуса — отдельный span: остаётся видимым, даже когда
            // плашка маркера «ныряет» в лоток при сбросе.
            const num = document.createElement('span');
            num.className = 'line-token-num';
            el.appendChild(num);
            // Появление корпуса на линии: плавное опускание сверху вниз в
            // свою ячейку (деталь «подаётся» на ленту). Горизонтального
            // вкатывания слева нет — маркер падает в ячейку сверху через
            // CSS-анимацию token-enter.
            el.classList.add('token-enter');
            el.style.left = `${box.left}px`;
            el.style.opacity = targetOpacity;
        } else {
            token.el.style.top = `${box.top}px`;
            token.el.style.width = `${box.width}px`;
            token.el.style.height = `${box.height}px`;
            const targetLeft = `${box.left}px`;
            if (token.el.style.left !== targetLeft) token.el.style.left = targetLeft;
            if (token.el.style.opacity !== '0') token.el.style.opacity = targetOpacity;
            token.position = targetPos;
        }
        // Backend marks a body as ``dropping`` as soon as the route is
        // prepared.  Keep the normal hold marker until the belt actually
        // carries it to +8; otherwise the token flashes a drop arrow at +7
        // before motion and can look as if it jumped through the gate.
        token.dropping = dropAnimationActive;
        token.held = !!meta.held;
        if (token.category !== meta.category) {
            token.category = meta.category;
            _applyTokenCategory(token.el, meta.category);
        }
        // Придержание, сброс и нахождение в лотке рисуются поверх цвета
        // категории, но не смешиваются между собой.
        token.el.classList.remove('token-hold', 'token-dropping');
        if (token.held) token.el.classList.add('token-hold');
        if (token.dropping) token.el.classList.add('token-dropping');

        // Сброс: элемент СНАЧАЛА доезжает до точки сброса (+8) горизонтально
        // (переход left длится `duration`), и ТОЛЬКО ПОТОМ падает вниз.
        // Класс token-in-chute (прозрачная плашка + падение token-sink)
        // добавляем по таймеру через `duration`, т.е. когда скольжение
        // завершилось. Флаг inChute защищает от повторного добавления на
        // каждом снимке статуса.
        if (token.position === 8) {
            if (!token.inChute) {
                token.inChute = true;
                const el = token.el;
                setTimeout(() => {
                    if (token && token.inChute && token.position === 8
                            && el && el.parentNode) {
                        el.classList.add('token-in-chute');
                    }
                }, duration);
            }
        } else if (token.inChute) {
            token.inChute = false;
            token.el.classList.remove('token-in-chute');
        }
        // Номер корпуса обновляем в span (всегда видим, даже при сбросе).
        let num = token.el.querySelector('.line-token-num');
        if (!num) {
            num = document.createElement('span');
            num.className = 'line-token-num';
            token.el.appendChild(num);
        }
        num.textContent = `#${id}`;
        token.el.title = `Корпус #${id} · ${categoryLabel(meta.category)}`;
    }

    // The chute marker is a real visual layer above the cell symbol. Keep
    // the symbol hidden while that layer is occupied, including the short
    // fade-out interval after a drop.
    _updateChuteOccupied(cells);

    if (!pendingAnalysis) {
        _appliedLineParts = (lineParts || []).map(part => ({
            id: Number(part.id),
            position: Math.max(0, Math.min(Number(part.position) || 0, 7)),
            category: (part.category || '').toUpperCase(),
        }));
        _appliedInLine = _appliedLineParts.length;
    }

    _lineSyncDone = true;
}

const PENDING_VISUAL_TIMEOUT_MS = 1500;
function armPendingFlushFallback() {
    if (state.pendingFlushTimer) clearTimeout(state.pendingFlushTimer);
    state.pendingFlushTimer = setTimeout(() => {
        state.pendingFlushTimer = null;
        flushPendingAnalysis();
    }, PENDING_VISUAL_TIMEOUT_MS);
}
function flushPendingAnalysis() {
    if (state.pendingAnalysisVersion === null) return;
    state.pendingAnalysisVersion = null;
    if (state.pendingFlushTimer) { clearTimeout(state.pendingFlushTimer); state.pendingFlushTimer = null; }
    const ls = state.lastLineStatus;
    if (ls) {
        updateLineCells(ls.line_parts || [], ls.process || {});
        if (typeof updateNewFrameAnalysisStatus === 'function') updateNewFrameAnalysisStatus(ls);
    }
    if (typeof refreshPreviewStrip === 'function') refreshPreviewStrip();
    if (typeof faSyncScroll === 'function') {
        try { requestAnimationFrame(() => faSyncScroll()); } catch (_) {}
    }
}
