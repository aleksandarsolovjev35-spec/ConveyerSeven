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
let _lineSyncDone = false;
let _appliedLineParts = [];
let _appliedInLine = 0;

function lineMoveDuration(process = {}) {
    const conv = process.conveyor || {};
    const speed = Number(conv.speed) || 0;
    if (!speed) return 420;
    return Math.max(265, Math.min(620, Math.round(8400000 / speed)));
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

function _updateLineGates(lineParts, process = {}) {
    const gateIn = document.querySelector('.line-gate-in');
    const gateOut = document.querySelector('.line-gate-out');
    if (!gateIn || !gateOut) return;
    gateIn.className = 'line-gate line-gate-in';
    gateOut.className = 'line-gate line-gate-out';
    const partAtInput = (lineParts || []).find(p => Number(p.position) === 0);
    if (partAtInput) gateIn.classList.add('gate-active');
    const partAtReject = (lineParts || []).find(p => Number(p.position) === 7);
    const outCat = partAtReject ? (partAtReject.category || '').toUpperCase() : '';
    const isDropPhase = (process.phase || '').includes('DROP') || (process.phase || '').includes('ROUTE') || (process.phase || '').includes('REJECT');
    if (outCat === 'BAD') { gateOut.classList.add('gate-rejecting'); gateOut.textContent = '▼ СБРОС'; }
    else if (outCat === 'CLEANUP') { gateOut.classList.add('gate-cleanup'); gateOut.textContent = '▼ ОЧИСТКА'; }
    else if (isDropPhase && partAtReject) { gateOut.classList.add('gate-rejecting'); gateOut.textContent = '▼ СБРОС'; }
    else {
        if (_currentDistributorCategory === 'BAD') { gateOut.classList.add('gate-rejecting'); gateOut.textContent = 'ВЫХОД ▸'; }
        else if (_currentDistributorCategory === 'CLEANUP') { gateOut.classList.add('gate-cleanup'); gateOut.textContent = 'ВЫХОД ▸'; }
        else { gateOut.classList.add('gate-active'); gateOut.textContent = 'ВЫХОД ▸'; }
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
    const isConveyorMoving = (process.phase || '').includes('CONVEYOR') || (process.phase || '').includes('MOTION') || (process.phase || '').includes('ROUTE_PREPARE');
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
        const activeText = process.phase ? (process.label || process.phase).slice(0, 64).toUpperCase() : '';
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
    const phase = process.phase || '';
    cells.forEach(cell => {
        const position = Number(cell.dataset.pos);
        cell.className = 'line-cell';
        if (active.includes(position)) {
            cell.classList.add('process-active');
            if (phase.includes('CAMERA') || phase.includes('ANALYSIS')) cell.classList.add('process-camera');
            if (phase.includes('ROUTE') || phase.includes('DROP')) cell.classList.add('process-route');
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
        wanted.set(id, {position: Math.max(0, Math.min(Number(part.position) || 0, 7)), category});
    }

    const {containerRect, rects} = _lineCellRects(cells);
    const geometryReady = !!(containerRect.width && rects[0] && rects[0].width);
    const step = (rects[0] && rects[1]) ? (rects[1].left - rects[0].left) : (rects[0] ? rects[0].width + 3 : 0);
    const duration = lineMoveDuration(process);

    for (const [id, token] of [..._lineTokens.entries()]) {
        if (wanted.has(id)) continue;
        _lineTokens.delete(id);
        if (geometryReady && rects[token.position]) {
            token.el.style.left = `${rects[token.position].left + step}px`;
            token.el.style.opacity = '0';
        }
        const el = token.el;
        setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, duration + 80);
    }

    if (!geometryReady) {
        for (const [id, meta] of wanted) {
            const token = _lineTokens.get(id);
            if (token) token.position = meta.position;
        }
        return;
    }

    for (const [id, meta] of wanted) {
        const target = rects[meta.position] || rects[0];
        let token = _lineTokens.get(id);
        if (!token) {
            const el = document.createElement('div');
            el.className = 'line-token';
            el.dataset.partId = String(id);
            el.style.top = `${target.top}px`;
            el.style.width = `${target.width}px`;
            el.style.height = `${target.height}px`;
            token = {el, position: meta.position, category: null};
            _lineTokens.set(id, token);
            els.lineCells.appendChild(el);
            if (_lineSyncDone) {
                el.style.left = `${target.left - step}px`;
                el.style.opacity = '0';
                requestAnimationFrame(() => { el.style.left = `${target.left}px`; el.style.opacity = '1'; });
            } else {
                el.style.left = `${target.left}px`;
                el.style.opacity = '1';
            }
        } else {
            token.el.style.top = `${target.top}px`;
            token.el.style.width = `${target.width}px`;
            token.el.style.height = `${target.height}px`;
            const targetLeft = `${target.left}px`;
            if (token.el.style.left !== targetLeft) token.el.style.left = targetLeft;
            token.position = meta.position;
        }
        if (token.category !== meta.category) {
            token.category = meta.category;
            _applyTokenCategory(token.el, meta.category);
        }
        token.el.textContent = `#${id}`;
        token.el.title = `Корпус #${id} · ${categoryLabel(meta.category)}`;
    }

    if (!pendingAnalysis) {
        _appliedLineParts = (lineParts || []).map(part => ({
            id: Number(part.id),
            position: Math.max(0, Math.min(Number(part.position) || 0, 7)),
            category: (part.category || '').toUpperCase(),
        }));
        _appliedInLine = _appliedLineParts.length;
    }

    _updateLineGates(lineParts, process);
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
