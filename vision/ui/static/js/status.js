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

    // Если за время запроса пришёл более свежий ответ (через immediate), старый игнорируем
    if (mySeq < _lastHandledStatusSeq) return;
    _lastHandledStatusSeq = mySeq;

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
    const liveInfo = lineStatusPayload.live || {};
    const staticPublish = (
        newPublishArrived
        && incomingVersion > 0
        && !state.splashActive
        && !liveInfo.streaming
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
    setIfChanged(els.stateLabel, lineStateLabel('OFFLINE'));
    if (els.stateSection) els.stateSection.className = 'state-section state-box-offline';
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

function updateLineStatus(ls) {
    const lineState = ls.state || 'IDLE';
    const exitRequested = !!ls.exit_requested;

    state.lineState = lineState;
    state.serverExitRequested = exitRequested;
    if (!['IDLE', 'STOPPED'].includes(lineState)) state.startPending = false;
    updateOperationalAccordions(lineState);

    if (els.stateIndicator) els.stateIndicator.className = `state-dot state-${lineState.toLowerCase()}`;
    if (els.stateSection) els.stateSection.className = `state-section state-box-${lineState.toLowerCase()}`;
    setIfChanged(els.stateLabel, lineStateLabel(lineState));
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

    const process = ls.process || {};
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
    const d1Moving = ['MOVING', 'OPENING', 'CLOSING', 'HOMING'].includes(String(d1State).toUpperCase());
    const d1TargetLabel = d1Moving ? 'ПЕРЕМЕЩЕНИЕ' : (d1Pos <= 0 ? 'ПРОХОД' : (d1Pos >= d1Max ? 'СБРОС' : `ПОЗИЦИЯ ${d1Pos}`));
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

function lineMoveDuration(process = {}) {
    const conv = process.conveyor || {};
    const speed = Number(conv.speed) || 0;
    if (!speed) return 420;
    return Math.max(265, Math.min(620, Math.round(8400000 / speed)));
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

function tapeShortLabel(process) {
    if (!process || !process.phase) return '';
    const p = String(process.phase).toUpperCase();
    const lbl = String(process.label || '').toLowerCase();
    // Короткие названия без лишних деталей (кандидат, счётчики, таймеры).
    if (isConveyorTransportPhase(p)) return 'ДВИЖЕНИЕ';
    if (p === 'CONVEYOR_CONFIRMED') return 'СТОЯНКА';
    if (p === 'PART_DROP') return 'СБРОС';
    if (p === 'PART_HOLD') {
        if (lbl.includes('серии')) return 'ОЖИДАНИЕ СБРОСА';
        if (lbl.includes('проход')) return 'ПРОХОД';
        return 'УДЕРЖАНИЕ';
    }
    if (p === 'SETTLE') return 'СТОЯНКА';
    if (p === 'CAMERA_CAPTURE') return 'СЪЁМКА';
    if (p === 'ANALYSIS_REVIEW') return 'ПАУЗА';
    if (p.includes('INPUT_ANALYSIS') || p === 'INPUT_ANALYSIS') return 'АНАЛИЗ ВХОДА';
    if (p.includes('SPIDER')) return 'АНАЛИЗ КОНТРОЛЯ';
    if (p.includes('ANALYSIS')) return 'АНАЛИЗ';
    if (p.includes('ROUTE')) return 'ВЫХОД';
    if (p === 'STEP_COMPLETE') return 'ГОТОВ';
    if (p === 'START_POSITIONING' || p === 'READY') return 'ГОТОВ';
    if (p === 'INITIAL_INSPECTION') return 'КОНТРОЛЬ';
    if (p === 'ROUTE_PREPARE') return 'ПОДГОТОВКА';
    if (p === 'DRAINING') return 'ЗАВЕРШЕНИЕ';
    if (p === 'STOPPED' || p === 'IDLE') return 'ОЖИДАНИЕ';
    if (p.includes('PAUSE')) return 'ПАУЗА';
    if (p.includes('JOG')) return 'РУЧНОЙ ХОД';
    // fallback — сама фаза коротко, без label с деталями
    return p.replace(/_/g, ' ').slice(0, 20);
}

function _lineCellRects(cells) {
    if (!els.lineCells) return {containerRect: {width: 0}, rects: {}};
    const containerRect = els.lineCells.getBoundingClientRect();
    const rects = {};
    cells.forEach(cell => {
        const r = cell.getBoundingClientRect();
        rects[Number(cell.dataset.pos)] = {
            left: r.left - containerRect.left,
            top: r.top - containerRect.top,
            width: r.width,
            height: r.height,
        };
    });
    return {containerRect, rects};
}

function _applyTokenCategory(el, category) {
    el.classList.remove('cell-good', 'cell-bad', 'cell-cleanup');
    if (category === 'BAD') el.classList.add('cell-bad');
    else if (category === 'CLEANUP') el.classList.add('cell-cleanup');
    else if (category === 'GOOD') el.classList.add('cell-good');
}

function _removeLineTokenElement(token) {
    if (!token) return;
    _lineExitTokens.delete(token);
    if (token.el && token.el.parentNode) token.el.parentNode.removeChild(token.el);
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

function _updateLineGates(lineParts, process = {}) {
    const gateIn = document.querySelector('.line-gate-in');
    const gateOut = document.querySelector('.line-gate-out');
    if (!gateIn || !gateOut) return;
    gateIn.className = 'line-gate line-gate-in';
    gateOut.className = 'line-gate line-gate-out';
    gateOut.textContent = 'ВЫХОД ▸';

    const parts = Array.isArray(lineParts) ? lineParts : [];
    const partAtInput = parts.find(p => Number(p.position) === 0);
    if (partAtInput) gateIn.classList.add('gate-active');

    const partAtSort = parts.find(p => Number(p.position) === 7);
    if (!partAtSort) {
        // После сброса канал распределителя ещё виден: держим цвет ворота,
        // чтобы оператор видел, куда только что ушёл корпус.
        if (_currentDistributorCategory === 'BAD') gateOut.classList.add('gate-rejecting');
        else if (_currentDistributorCategory === 'CLEANUP') gateOut.classList.add('gate-cleanup');
        else gateOut.classList.add('gate-active');
        return;
    }

    const cat = String(partAtSort.category || '').toUpperCase();
    const phase = String(process.phase || '').toUpperCase();
    const held = !!partAtSort.held;
    const dropping = !!partAtSort.dropping;
    const routing = phase.includes('ROUTE') || phase.includes('DROP')
        || phase.includes('CONVEYOR') || phase.includes('PART_HOLD');
    // Корпус на сортировке (+7) придержан лепестком или уже падает в
    // канал: ворота показывают канал маршрута (БРАК/ОЧИСТКА). Годный
    // корпус проходит без сброса — ворота показывают «проход» весь
    // период пребывания годного на +7, а не только во время ROUTE.
    if (cat === 'BAD' && (held || dropping || routing)) {
        gateOut.classList.add('gate-rejecting');
        gateOut.textContent = '▼ БРАК';
    } else if (cat === 'CLEANUP' && (held || dropping || routing)) {
        gateOut.classList.add('gate-cleanup');
        gateOut.textContent = '▼ ОЧИСТКА';
    } else if (cat === 'GOOD') {
        gateOut.classList.add('gate-active');
        gateOut.textContent = 'ПРОХОД ▸';
    } else {
        gateOut.classList.add('gate-active');
        gateOut.textContent = 'ВЫХОД ▸';
    }
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
        const d1Open = ['OPEN', 'OPENING'].includes(d1State) || (d1State !== 'CLOSING' && Number(ls.dist1_position || 0) > 0);
        if (d1Open) category = String(ls.dist2_target || '').toUpperCase() === 'CLEANUP' ? 'CLEANUP' : 'BAD';
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
        const closingToHome = d1State === 'CLOSING';
        const parked = ['IDLE', 'STOPPED'].includes(lineState) && (d1State === 'IDLE' || closingToHome) && (Number(ls.dist1_position || 0) === 0 || closingToHome);
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
    const isConveyorMoving = isConveyorTransportPhase(phase);
    let belt = els.lineCells.querySelector('.conveyor-belt');
    if (!belt) {
        belt = document.createElement('div');
        belt.className = 'conveyor-belt';
        els.lineCells.insertBefore(belt, els.lineCells.firstChild);
    }
    belt.classList.toggle('moving', !!isConveyorMoving);
    els.lineCells.style.setProperty('--move-duration', `${lineMoveDuration(process)}ms`);

    const phaseEl = els.processPhaseLabel || document.getElementById('process-phase-label');
    if (phaseEl) {
        const short = tapeShortLabel(process);
        const activeText = short ? short.toUpperCase() : '';
        if (activeText) {
            setIfChanged(phaseEl, activeText);
            phaseEl.style.opacity = isConveyorMoving ? '1' : '0.85';
            phaseEl.style.color = isConveyorMoving ? 'var(--ok)' : 'var(--accent)';
        } else {
            const defaultLabel = state.lineState === 'RUNNING' ? 'РАБОТА' : 'ОЖИДАНИЕ ПУСКА';
            setIfChanged(phaseEl, defaultLabel);
            phaseEl.style.opacity = '0.65';
            phaseEl.style.color = 'var(--text-dim)';
        }
    }

    const cells = els.lineCells.querySelectorAll('.line-cell[data-pos]');
    const active = Array.isArray(process.positions) ? process.positions : [];
    const phaseUpper = phase;
    cells.forEach(cell => {
        const position = Number(cell.dataset.pos);
        cell.className = 'line-cell';
        if (position === 8) cell.classList.add('line-cell-chute');
        if (active.includes(position)) {
            cell.classList.add('process-active');
            if (phase.includes('CAMERA') || phase.includes('ANALYSIS')) cell.classList.add('process-camera');
            if (phase.includes('ROUTE') || phase.includes('DROP')) cell.classList.add('process-route');
        }
    });

    // Сортировка +7 по фактической логике: корпус ДОЕХАЛ и придержан
    // лепестком (held) либо уже падает в лоток (dropping) — ячейка +7
    // получает ограничитель, лоток +8 — цвет канала маршрута.
    // При серии через пустую ячейку лоток сохраняет цвет канала
    // (DIST2 не уходит, DIST1 остаётся открытой), как и ворота выхода.
    const sortPart = (lineParts || []).find(p => Number(p.position) === 7);
    const sortHeld = !!(sortPart && sortPart.held);
    const sortCat = sortPart ? String(sortPart.category || '').toUpperCase() : '';
    let chuteCat = sortCat;
    if (!sortPart) {
        // Пауза серии с пустой ячейкой: лоток держит канал распределителя,
        // чтобы оператор видел, куда пойдёт следующий корпус той же категории.
        if (_currentDistributorCategory === 'BAD') chuteCat = 'BAD';
        else if (_currentDistributorCategory === 'CLEANUP') chuteCat = 'CLEANUP';
    }
    cells.forEach(cell => {
        const position = Number(cell.dataset.pos);
        if (position === 7 && sortHeld) cell.classList.add('cell-hold');
        if (position === 8) {
            if (chuteCat === 'BAD') cell.classList.add('chute-bad');
            else if (chuteCat === 'CLEANUP') cell.classList.add('chute-cleanup');
        }
    });

    // Gate labels do not depend on layout measurements. Update them before
    // the geometry guard so a cold first render still shows the route while
    // the browser is calculating the grid rectangles.
    _updateLineGates(lineParts, process);

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

    for (const [id, token] of [..._lineTokens.entries()]) {
        if (wanted.has(id)) continue;
        _lineTokens.delete(id);
        token.exitPosition = token.dropping ? 8 : null;
        _lineExitTokens.add(token);
        if (token.dropping) {
            // Падение: маркер уже уехал в лоток +8 — гаснет на месте.
            token.el.style.opacity = '0';
        } else if (geometryReady && rects[token.position]) {
            // Проход (годный) или съезд с линии: маркер уезжает за ячейку.
            // Годный корпус на сортировке уходит через ворота выхода, а не в лоток:
            // уезжает дальше лотка, под ворота, и там гаснет.
            let exitLeft;
            if (token.position === 7 && token.category === 'GOOD' && rects[8]) {
                exitLeft = rects[8].left + step;
            } else {
                exitLeft = rects[token.position].left + step;
            }
            token.el.style.left = `${exitLeft}px`;
            token.el.style.opacity = '0';
        }
        setTimeout(() => {
            _removeLineTokenElement(token);
            _updateChuteOccupied(cells);
        }, duration + 80);
    }

    if (!geometryReady) {
        for (const [id, meta] of wanted) {
            const token = _lineTokens.get(id);
            if (token) token.position = meta.position;
        }
        _updateChuteOccupied(cells);
        return;
    }

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
        // Полностью непрозрачный маркер: падающий корпус не должен
        // «просвечивать» цвет канала лотка (тот же цвет, что и у самого
        // маркера) — иначе на +7/+8 получается двойная заливка одного цвета.
        const targetOpacity = '1';
        if (!token) {
            const el = document.createElement('div');
            el.className = 'line-token';
            el.dataset.partId = String(id);
            el.style.top = `${target.top}px`;
            el.style.width = `${target.width}px`;
            el.style.height = `${target.height}px`;
            token = {el, position: targetPos, category: null, dropping: false, held: false};
            _lineTokens.set(id, token);
            els.lineCells.appendChild(el);
            if (_lineSyncDone) {
                el.style.left = `${target.left - step}px`;
                el.style.opacity = '0';
                requestAnimationFrame(() => {
                    el.style.left = `${target.left}px`;
                    el.style.opacity = targetOpacity;
                });
            } else {
                el.style.left = `${target.left}px`;
                el.style.opacity = targetOpacity;
            }
        } else {
            token.el.style.top = `${target.top}px`;
            token.el.style.width = `${target.width}px`;
            token.el.style.height = `${target.height}px`;
            const targetLeft = `${target.left}px`;
            if (token.el.style.left !== targetLeft) token.el.style.left = targetLeft;
            if (token.el.style.opacity !== '0') token.el.style.opacity = targetOpacity;
            token.position = targetPos;
        }
        // Backend marks a body as ``dropping`` as soon as the route is
        // prepared.  Keep the normal hold marker until the belt actually
        // carries it to +8; otherwise the token flashes a drop arrow at +7
        // before motion and can look as if it jumped through the gate.
        token.dropping = dropAnimationActive;
        token.held = !!meta.held || (!!meta.dropping && !dropAnimationActive);
        if (token.category !== meta.category) {
            token.category = meta.category;
            _applyTokenCategory(token.el, meta.category);
        }
        // Придержание, сброс и нахождение в лотке рисуются поверх цвета
        // категории, но не смешиваются между собой.
        token.el.classList.remove('token-hold', 'token-dropping', 'token-in-chute');
        if (token.held) token.el.classList.add('token-hold');
        if (token.dropping) token.el.classList.add('token-dropping');
        if (token.position === 8) token.el.classList.add('token-in-chute');
        token.el.textContent = `#${id}`;
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
