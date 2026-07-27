// status.js — Line Monitor UI module
'use strict';

// ─── Status update ───────────────────────────────────────────

function updateOperationalAccordions(lineState) {
    const fullyStopped = lineState === 'IDLE' || lineState === 'STOPPED';

    if (els.statsBody) {
        els.statsBody.classList.remove('is-collapsed');
    }
    if (els.statsSummary) {
        els.statsSummary.classList.add('is-open');
    }
    if (els.statsService) {
        els.statsService.classList.remove('is-collapsed');
    }
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.toggle(
            'controls-collapsed',
            !fullyStopped,
        );
        els.distributorDiagnostics
            .querySelectorAll('.blade-diagnostic-grid')
            .forEach(grid => {
                grid.classList.toggle('is-collapsed', !fullyStopped);
            });
    }
}

function setBladeMarkerPosition(marker, percent) {
    if (!marker) return;
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    marker.style.left = `${normalized}%`;
}

async function fetchStatus() {
    if (!state.bootDone || state.statusFetchBusy) return;
    state.statusFetchBusy = true;

    const status = await apiGet('/api/status');
    state.statusFetchBusy = false;
    if (!status) {
        const reference = state.lastStatusAt || state.bootDoneAt;
        if (reference > 0 && Date.now() - reference >= STATUS_OFFLINE_AFTER) {
            markUiOffline();
        }
        return;
    }

    state.lastStatusAt = Date.now();
    state.statusReceived = true;
    if (state.offline) {
        state.offline = false;
        els.main.classList.remove('ui-offline');
        clearControlError();
    }

    if (
        status.frame_versions
        && typeof status.frame_versions === 'object'
    ) {
        state.frameVersions = {...status.frame_versions};
    }

    if (typeof status.frame_version === 'number') {
        state.currentVersion = status.frame_version;
        if (
            state.currentVersion !== state.lastSeenVersion
            && state.mainCamMode === 'pull'
            && !state.splashActive
        ) {
            maybeRequestMainFrame();
        }
    }

    const oldState = state.lineState;
    updateLineStatus(status.line_status || {});
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
    if (typeof clearLivePullTimer === 'function') {
        clearLivePullTimer();
    }
    state.mainCamMode = 'pull';
    mainBufferLoading = false;
    els.main.classList.add('ui-offline');
    setIfChanged(els.stateLabel, lineStateLabel('OFFLINE'));
    if (els.stateSection) {
        els.stateSection.className = 'state-section state-box-offline';
    }
    showControlError('Нет связи с backend. Все команды заблокированы.');
    releaseJogHoldBestEffort('backend offline');
    applyButtonsForState('OFFLINE', true, {});
    updateDistributorDiagnosticControls({
        diagnostic_allowed: false,
        diagnostic_busy: true,
    });
    disablePrestartDiagnosticButtons();
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
    updateViewModeControls();
    if (els.jogPanel) {
        els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(button => {
            button.disabled = true;
        });
    }
    updateStateOverlay({state: 'OFFLINE', in_line: 0});
}

function updateLineStatus(ls) {
    const lineState     = ls.state || 'IDLE';
    const exitRequested = !!ls.exit_requested;

    state.lineState           = lineState;
    state.serverExitRequested = exitRequested;
    if (!['IDLE', 'STOPPED'].includes(lineState)) {
        state.startPending = false;
    }
    updateOperationalAccordions(lineState);

    els.stateIndicator.className =
        `state-dot state-${lineState.toLowerCase()}`;
    if (els.stateSection) {
        els.stateSection.className =
            `state-section state-box-${lineState.toLowerCase()}`;
    }
    setIfChanged(els.stateLabel, lineStateLabel(lineState));
    setIfChanged(els.metricStep, ls.step || 0);

    state.backendControls = ls.controls || {};
    applyButtonsForState(
        lineState,
        exitRequested,
        state.backendControls,
    );
    updateViewModeControls();

    setIfChanged(els.statTotal,   ls.total    || 0);
    setIfChanged(els.statGood,    ls.good     || 0);
    setIfChanged(els.statBad,     ls.rejected || 0);
    setIfChanged(els.statCleanup, ls.cleanup  || 0);
    setIfChanged(els.statEmpty,   ls.empty    || 0);

    const inLine = ls.in_line || 0;
    setIfChanged(els.statInline, `${inLine} / 8`);

    const process = ls.process || {};
    updateLineCells(ls.line_parts || [], process);
    updateDistributorDiagnosticControls(ls);
    updatePrestartDiagnostics(ls);
    updateSelectedAnalysisStatus(ls);
    updateFrameAnalysisStatus(ls);

    const d1State = ls.dist1_state || 'IDLE';
    els.dist1State.className =
        `axis-state axis-${d1State.toLowerCase()}`;
    setIfChanged(els.dist1State, axisStateLabel(d1State));
    const d1Pos = Math.max(0, Number(ls.dist1_position || 0));
    const d1Max = Math.max(1, Number(ls.dist1_max || 340));
    setIfChanged(els.dist1Pos, d1Pos);
    setIfChanged(els.dist1Max, d1Max);
    if (els.dist1Blade) {
        const d1Percent = Math.max(0, Math.min(100, d1Pos / d1Max * 100));
        setBladeMarkerPosition(els.dist1Blade, d1Percent);
    }
    const d1Moving = ['MOVING', 'OPENING', 'CLOSING', 'HOMING'].includes(
        String(d1State).toUpperCase(),
    );
    const d1TargetLabel = d1Moving
        ? 'ПЕРЕМЕЩЕНИЕ'
        : (d1Pos <= 0 ? 'ПРОХОД' : (d1Pos >= d1Max ? 'СБРОС' : `ПОЗИЦИЯ ${d1Pos}`));
    setIfChanged(els.dist1Target, d1TargetLabel);

    const d2State = ls.dist2_state || 'IDLE';
    els.dist2State.className =
        `axis-state axis-${d2State.toLowerCase()}`;
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

    const action = ls.last_distributor_action;
    setIfChanged(
        els.distAction,
        distributorActionLabel(action),
    );

    updateJogState(ls.jog || null);
    updateStateOverlay(ls);
    updateJogHardware(ls);
    handleJogAutoToggle(lineState, ls.jog || null);
}

// ─── Line cells ──────────────────────────────────────────────

function updateLineCells(lineParts, process = {}) {
    const cells = els.lineCells.children;

    for (let i = 0; i < cells.length; i++) {
        const cell = cells[i];
        const part = lineParts.find(p => p.position === i);

        cell.className = 'line-cell';
        cell.textContent = part ? `№${part.id}` : '';

        const activePositions = Array.isArray(process.positions)
            ? process.positions
            : [];
        if (activePositions.includes(i)) {
            cell.classList.add('process-active');
            if ((process.phase || '').includes('CAMERA')
                || (process.phase || '').includes('ANALYSIS')) {
                cell.classList.add('process-camera');
            }
            if ((process.phase || '').includes('ROUTE')
                || (process.phase || '').includes('DROP')) {
                cell.classList.add('process-route');
            }
        }

        if (part) {
            cell.classList.add('occupied');

            const cat = (part.category || '').toUpperCase();
            if (cat === 'BAD') {
                cell.classList.add('cell-bad');
            } else if (cat === 'CLEANUP') {
                cell.classList.add('cell-cleanup');
            } else if (cat === 'GOOD') {
                cell.classList.add('cell-good');
            }

            cell.title = `Деталь №${part.id} · ${categoryLabel(part.category)}`;
        } else {
            cell.title = '';
        }
    }
}
