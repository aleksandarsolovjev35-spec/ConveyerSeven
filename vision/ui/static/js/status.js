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
    const staticRoles = Array.isArray(liveInfo.static_roles) ? liveInfo.static_roles : [];
    const processInfo = lineStatusPayload.process || {};
    const inspectionRoles = Array.isArray(processInfo.inspection_roles) ? processInfo.inspection_roles : [];
    const processPhase = String(processInfo.phase || '').toUpperCase();
    const inspectionDisplay = inspectionRoles.includes(state.currentCamera)
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
    setIfChanged(els.stateLabel, lineStateLabel('OFFLINE'));
    if (els.stateSection) els.stateSection.className = 'state-section state-box-offline';
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

function updateProcessPhaseLabel(lineState) {
    const phaseEl = els.processPhaseLabel || document.getElementById('process-phase-label');
    if (!phaseEl) return;
    const activeState = String(lineState || 'IDLE').toUpperCase();
    setIfChanged(phaseEl, lineStateLabel(activeState));
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

    if (els.stateIndicator) els.stateIndicator.className = `state-dot state-${lineState.toLowerCase()}`;
    if (els.stateSection) els.stateSection.className = `state-section state-box-${lineState.toLowerCase()}`;
    setIfChanged(els.stateLabel, lineStateLabel(lineState));
    updateProcessPhaseLabel(lineState);
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
// Bodies removed by the backend at +7 remain physically on the stencil until
// the next conveyor step carries them to +8.
const _lineDepartingTokens = new Set();
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

function _removeStencilToken(token) {
    (token.pieces || []).forEach(piece => piece.remove());
    if (token.exitTimer) clearTimeout(token.exitTimer);
}

function updateLineCells(lineParts, process = {}) {
    if (!els.lineCells) return;
    const cells = [...els.lineCells.querySelectorAll('.line-cell[data-pos]')];
    const {containerRect, rects} = _lineCellRects(cells);
    if (!containerRect.width || !rects[0] || !rects[0].width) return;

    const phase = String(process.phase || '').toUpperCase();
    const moving = isConveyorTransportPhase(phase);
    const duration = lineMoveDuration(process);
    els.lineCells.style.setProperty('--move-duration', `${duration}ms`);

    const pendingAnalysis = state.pendingAnalysisVersion !== null;
    const appliedById = new Map(_appliedLineParts.map(part => [part.id, part.category]));
    const wanted = new Map();
    for (const part of lineParts || []) {
        const id = Number(part.id);
        if (!Number.isFinite(id) || (pendingAnalysis && !appliedById.has(id))) continue;
        const category = pendingAnalysis && appliedById.has(id)
            ? appliedById.get(id) : String(part.category || '').toUpperCase();
        let position = Math.max(0, Math.min(Number(part.position) || 0, 7));
        const wasInDropWindow = _lineTokens.get(id)?.position === 8;
        // Статус во время хода относится к позиции до подтверждения остановки.
        // Визуально все корпуса делают один и тот же непрерывный шаг. После
        // подтверждения корпус остаётся виден в +8 до отдельного падения.
        if (moving) position = part.dropping ? 8 : Math.min(position + 1, 8);
        else if (part.dropping && wasInDropWindow) position = 8;
        wanted.set(id, {position, category, dropping: !!part.dropping});
    }

    // Удалённый из статуса корпус уже стоит в +8: только теперь он падает
    // под вагон. Никакого исчезновения или выхода во время горизонтального
    // шага нет.
    for (const [id, token] of [..._lineTokens.entries()]) {
        if (wanted.has(id)) continue;
        _lineTokens.delete(id);
        if (token.position === 8) {
            token.pieces.forEach(piece => piece.classList.add('token-exiting'));
            token.exitTimer = setTimeout(() => _removeStencilToken(token), duration);
        } else if (token.position === 7) {
            // The logical list may release a body at sorting before the next
            // step. Keep its physical body visible until that step reaches +8.
            token.departing = true;
            token.movedToExit = false;
            _lineDepartingTokens.add(token);
        } else {
            _removeStencilToken(token);
        }
    }

    for (const token of [..._lineDepartingTokens]) {
        token.previousPosition = token.position;
        if (moving && token.position === 7) {
            token.position = 8;
            token.movedToExit = true;
        } else if (!moving && token.position === 8 && token.movedToExit && !token.exitTimer) {
            token.pieces.forEach(piece => piece.classList.add('token-exiting'));
            token.exitTimer = setTimeout(() => {
                _lineDepartingTokens.delete(token);
                _removeStencilToken(token);
            }, duration);
        }
    }

    for (const [id, meta] of wanted) {
        let token = _lineTokens.get(id);
        if (!token) {
            token = {id, position: meta.position, category: meta.category, pieces: [], entering: _lineSyncDone};
            _lineTokens.set(id, token);
        }
        token.previousPosition = token.position;
        token.position = meta.position;
        token.category = meta.category;
        token.dropping = meta.dropping;
    }

    // Трафарет: корпус находится за стенкой. Для каждого окна создаётся
    // обрезанный фрагмент корпуса. Если в прорезь одновременно попадают два
    // корпуса, фрагменты обоих не рисуются — окно остаётся пустым.
    const bodyWidth = rects[0].width * 0.78;
    const visibleByCell = new Map();
    const visualTokens = [..._lineTokens.values(), ..._lineDepartingTokens];
    for (const token of visualTokens) {
        const center = rects[token.position].left + rects[token.position].width / 2;
        const left = center - bodyWidth / 2;
        token.bodyLeft = left;
        for (let pos = 0; pos <= 8; pos += 1) {
            const r = rects[pos];
            if (Math.min(left + bodyWidth, r.left + r.width) > Math.max(left, r.left)) {
                const list = visibleByCell.get(pos) || [];
                list.push(token);
                visibleByCell.set(pos, list);
            }
        }
    }

    for (const token of visualTokens) {
        const relevant = new Set();
        [token.previousPosition, token.position].forEach(pos => {
            for (let i = Math.max(0, pos - 1); i <= Math.min(8, pos + 1); i += 1) relevant.add(i);
        });
        const previous = new Map(token.pieces.map(piece => [Number(piece.parentElement.dataset.pos), piece]));
        const nextPieces = [];
        for (const pos of relevant) {
            const cell = cells.find(item => Number(item.dataset.pos) === pos);
            if (!cell) continue;
            let piece = previous.get(pos);
            if (!piece) {
                piece = document.createElement('div');
                piece.className = 'line-token-piece';
                piece.dataset.partId = String(token.id);
                cell.appendChild(piece);
                if (token.entering && token.position === 0) piece.classList.add('token-entering');
            }
            const conflict = (visibleByCell.get(pos) || []).length > 1;
            piece.classList.toggle('is-hidden-by-conflict', conflict);
            piece.classList.remove('cell-good', 'cell-bad', 'cell-cleanup', 'token-exiting');
            _applyTokenCategory(piece, token.category);
            piece.style.width = `${bodyWidth}px`;
            piece.style.left = `${token.bodyLeft - rects[pos].left}px`;
            piece.title = `Корпус #${token.id} · ${categoryLabel(token.category)}`;
            nextPieces.push(piece);
        }
        token.pieces.forEach(piece => { if (!nextPieces.includes(piece)) piece.remove(); });
        token.pieces = nextPieces;
        if (token.entering) {
            requestAnimationFrame(() => token.pieces.forEach(piece => piece.classList.remove('token-entering')));
            token.entering = false;
        }
    }

    if (!pendingAnalysis) {
        _appliedLineParts = (lineParts || []).map(part => ({
            id: Number(part.id), position: Number(part.position) || 0,
            category: String(part.category || '').toUpperCase(),
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
