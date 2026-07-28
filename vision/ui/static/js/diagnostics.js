// diagnostics.js — Line Monitor UI module
'use strict';

function disablePrestartDiagnosticButtons() {
    if (!els.prestartDiagnostics) return;
    els.checkCameras.disabled = true;
    els.checkVisionRules.disabled = true;
}

function disableSelectedAnalysisButton() {
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
}

function updatePrestartDiagnostics(ls) {
    if (!els.prestartDiagnostics) return;
    const controls = ls.controls || {};
    const busy = (
        state.prestartDiagnosticPending
        || ls.diagnostic_busy === true
        || state.controlPending
    );
    const cameraAllowed = (
        controls.camera_diagnostic === true
        && !busy
        && !state.offline
    );
    const visionAllowed = (
        controls.vision_rule_diagnostic === true
        && !busy
        && !state.offline
    );
    els.checkCameras.disabled = !cameraAllowed;
    els.checkVisionRules.disabled = !visionAllowed;
    els.checkCameras.classList.toggle('pending', busy);
    els.checkVisionRules.classList.toggle('pending', busy);

    const report = ls.diagnostics || {};
    const status = report.status || 'NOT_RUN';
    els.diagnosticStatus.className = status;
    setIfChanged(els.diagnosticStatus, diagnosticStatusLabel(status));
    setIfChanged(els.diagnosticMessage, report.message || '—');
    const cameras = Array.isArray(report.cameras) ? report.cameras : [];
    const models = Array.isArray(report.models) ? report.models : [];
    const rules = Array.isArray(report.rules) ? report.rules : [];
    setIfChanged(
        els.diagnosticCameraCount,
        cameras.length ? `${cameras.filter(item => item.ok).length}/${cameras.length}` : '—',
    );
    setIfChanged(
        els.diagnosticModelCount,
        models.length ? `${models.filter(item => item.ok).length}/${models.length}` : '—',
    );
    setIfChanged(els.diagnosticRuleCount, rules.length || '—');
    setIfChanged(
        els.diagnosticTriggeredCount,
        rules.length ? rules.filter(item => item.triggered).length : '—',
    );
    const renderKey = [
        status,
        report.kind || '',
        report.updated_at || '',
        cameras.length,
        models.length,
        rules.length,
    ].join(':');
    if (state.lastDiagnosticRenderKey !== renderKey) {
        state.lastDiagnosticRenderKey = renderKey;
        renderDiagnosticDetails(cameras, models, rules);
    }
}

function renderDiagnosticDetails(cameras, models, rules) {
    if (!els.diagnosticDetails) return;
    const rows = [];
    for (const camera of cameras) {
        rows.push({
            left: cameraRoleLabel(camera.role),
            right: camera.ok
                ? `${camera.width}×${camera.height}${camera.detections === undefined ? '' : ` · ОБЪЕКТОВ ${camera.detections}`}`
                : 'ОШИБКА',
            kind: camera.ok ? '' : 'error',
        });
    }
    for (const model of models) {
        const name = String(model.model || '').split('/').pop();
        rows.push({
            left: `${cameraRoleLabel(model.role)} · ${name}`,
            right: model.ok
                ? `${Number(model.elapsed_ms || 0).toFixed(0)} мс · ОБЪЕКТОВ ${model.detections || 0}`
                : (model.error || 'ОШИБКА'),
            kind: model.ok ? '' : 'error',
        });
    }
    for (const rule of rules) {
        rows.push({
            left: `ПРАВИЛО · ${rule.name}`,
            right: rule.detail || (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА'),
            kind: rule.triggered ? 'triggered' : '',
        });
    }
    els.diagnosticDetails.replaceChildren();
    for (const row of rows) {
        const element = document.createElement('div');
        element.className = `diagnostic-detail-row ${row.kind}`.trim();
        const left = document.createElement('span');
        const right = document.createElement('b');
        left.textContent = row.left;
        right.textContent = row.right;
        element.append(left, right);
        els.diagnosticDetails.appendChild(element);
    }
}

function setupPrestartDiagnostics() {
    if (!els.prestartDiagnostics) return;
    els.checkCameras.addEventListener('click', () => {
        runPrestartDiagnostic('CAMERAS');
    });
    els.checkVisionRules.addEventListener('click', () => {
        runPrestartDiagnostic('VISION_RULES');
    });
}

async function runPrestartDiagnostic(kind) {
    if (
        state.prestartDiagnosticPending
        || state.offline
        || state.controlPending
    ) return;
    const button = kind === 'CAMERAS'
        ? els.checkCameras
        : els.checkVisionRules;
    if (!button || button.disabled) return;
    state.prestartDiagnosticPending = true;
    state.backendControls = {
        ...state.backendControls,
        start: false,
        exit: false,
        jog_hold: false,
        distributor_diagnostic: false,
        camera_diagnostic: false,
        vision_rule_diagnostic: false,
    };
    clearControlError();
    applyButtonsForState(
        state.lineState,
        state.serverExitRequested,
        state.backendControls,
    );
    if (els.jogPanel) {
        els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(
            jogButton => { jogButton.disabled = true; }
        );
    }
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.querySelectorAll('button').forEach(
            diagnosticButton => { diagnosticButton.disabled = true; }
        );
    }
    disableSelectedAnalysisButton();
    updatePrestartDiagnostics({
        controls: {},
        diagnostic_busy: true,
        diagnostics: {
            status: 'RUNNING',
            message: kind === 'CAMERAS'
                ? 'Проверка семи камер'
                : 'Камеры -> модели -> defect rules',
        },
    });
    const endpoint = kind === 'CAMERAS'
        ? '/api/diagnostics/cameras'
        : '/api/diagnostics/vision-rules';
    try {
        await apiPost(endpoint, true);
    } finally {
        state.prestartDiagnosticPending = false;
        requestImmediateStatus();
    }
}

function updateDistributorDiagnosticControls(ls) {
    if (!els.distributorDiagnostics) return;
    const allowed = (
        ls.diagnostic_allowed === true
        && (!ls.controls || ls.controls.distributor_diagnostic === true)
        && !state.offline
    );
    state.distributorDiagnosticBackendBusy = ls.diagnostic_busy === true;
    const busy = (
        state.distributorDiagnosticBackendBusy
        || state.distributorDiagnosticPending
    );
    els.distributorDiagnostics.querySelectorAll('button').forEach(button => {
        button.disabled = !allowed || busy;
        button.classList.toggle('pending', busy);
    });
}

function setupDistributorDiagnostics() {
    if (!els.distributorDiagnostics) return;
    els.distributorDiagnostics.querySelectorAll('button').forEach(button => {
        button.addEventListener('click', async () => {
            if (button.disabled || state.distributorDiagnosticPending) return;
            const command = button.dataset.distributorCommand;
            if (!command) return;
            state.distributorDiagnosticPending = true;
            startStatusPolling();
            updateViewModeControls();
            state.backendControls = {
                ...state.backendControls,
                start: false,
                exit: false,
                jog_hold: false,
                distributor_diagnostic: false,
                camera_diagnostic: false,
                vision_rule_diagnostic: false,
            };
            applyButtonsForState(
                state.lineState,
                state.serverExitRequested,
                state.backendControls,
            );
            if (els.jogPanel) {
                els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(
                    jogButton => { jogButton.disabled = true; }
                );
            }
            disablePrestartDiagnosticButtons();
            disableSelectedAnalysisButton();
            updateDistributorDiagnosticControls({
                diagnostic_allowed: false,
                diagnostic_busy: true,
            });
            try {
                clearControlError();
                await apiPost(
                    `/api/distributor/diagnostic/${command}`,
                    true,
                );
            } finally {
                state.distributorDiagnosticPending = false;
                startStatusPolling();
                updateViewModeControls();
                requestImmediateStatus();
            }
        });
    });
}

function updateSelectedAnalysisStatus(ls) {
    if (!els.analyzeSelectedFrame) return;
    const selected = ls.selected_analysis || {};
    const wasActive = state.selectedAnalysisActive;
    state.selectedAnalysisActive = selected.active === true;
    state.selectedAnalysisRole = selected.role || null;
    const live = ls.live || {};
    // fps публикуется и в live-блоке, и в jog для обратной совместимости.
    state.liveFps = Number(live.fps || (ls.jog || {}).live_fps || 0);
    state.liveStreaming = live.streaming === true;
    state.liveStatic = live.static === true;

    const controls = ls.controls || {};
    const allowed = state.selectedAnalysisActive
        ? controls.selected_model_release === true
        : controls.selected_model_analysis === true;
    els.analyzeSelectedFrame.disabled = (
        !allowed
        || state.selectedAnalysisPending
        || state.offline
        || !state.currentCamera
    );
    els.analyzeSelectedFrame.textContent = state.selectedAnalysisActive
        ? 'ВЕРНУТЬ ПОТОК'
        : 'АНАЛИЗ 3 КАДРОВ';
    els.analyzeSelectedFrame.classList.toggle(
        'analysis-active',
        state.selectedAnalysisActive,
    );
    applyLiveBadge(state.jogActive);
    updateViewModeControls();

    if (state.selectedAnalysisActive) {
        showSelectedAnalysisFrame(state.selectedAnalysisRole);
    } else if (wasActive) {
        returnSelectedCameraToLive();
    }
}

function updateFrameAnalysisStatus(ls) {
    if (!els.frameAnalysisPanel) return;
    const report = ls.frame_analysis || {};
    const available = report.available === true;
    const selectedActive = (
        state.selectedAnalysisActive
        && JOG_ALLOWED_STATES.includes(state.lineState)
    );
    const production = ['RUNNING', 'STOPPING'].includes(state.lineState);

    els.statsPanel.classList.toggle('production-view', production);
    els.statsPanel.classList.toggle('selected-frame-view', selectedActive);
    els.frameAnalysisPanel.classList.toggle('is-collapsed', !available);
    if (els.statsSummary) {
        els.statsSummary.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.statsService) {
        els.statsService.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.defectsSection && selectedActive) {
        els.defectsSection.classList.add('is-hidden');
    }

    if (!available) {
        state.lastFrameAnalysisRenderKey = null;
        return;
    }

    const models = Array.isArray(report.models) ? report.models : [];
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const context = report.role
        ? cameraRoleLabel(report.role)
        : (report.part_id ? `ДЕТАЛЬ №${report.part_id}` : 'ТЕКУЩИЙ ЦИКЛ');
    setIfChanged(els.frameAnalysisTitle, report.title || 'АНАЛИЗ 3 КАДРОВ');
    setIfChanged(els.frameAnalysisContext, context);
    setIfChanged(
        els.frameAnalysisMessage,
        report.message || 'Ожидание результатов анализа',
    );
    setIfChanged(els.frameAnalysisModelsTitle, `МОДЕЛИ · ${models.length}`);
    setIfChanged(els.frameAnalysisRulesTitle, `ПРАВИЛА · ${rules.length}`);

    const renderKey = JSON.stringify({
        kind: report.kind,
        role: report.role,
        part: report.part_id,
        updated: report.updated_at,
        models,
        rules,
    });
    if (state.lastFrameAnalysisRenderKey === renderKey) return;
    state.lastFrameAnalysisRenderKey = renderKey;

    renderFrameAnalysisModels(models);
    renderFrameAnalysisRules(rules);
    animateUiElement(els.frameAnalysisModels, 'ui-content-change');
    animateUiElement(els.frameAnalysisRules, 'ui-content-change');
}

function renderFrameAnalysisModels(models) {
    els.frameAnalysisModels.replaceChildren();
    if (!models.length) {
        appendFrameAnalysisEmpty(
            els.frameAnalysisModels,
            'Ожидание результатов моделей',
        );
        return;
    }
    for (const model of models) {
        const item = document.createElement('div');
        item.className = `frame-analysis-item ${model.ok ? 'ok' : 'error'}`;
        const name = document.createElement('span');
        const result = document.createElement('b');
        const fileName = String(model.model || '').split('/').pop();
        const runCount = Number(model.runs || 0);
        const runLabel = runCount > 1 ? `${runCount} ПРОГОНА · ` : '';
        name.textContent = `${runLabel}${cameraRoleLabel(model.role)} · ${fileName}`;
        const detectionsByRun = Array.isArray(model.detections_by_run)
            ? model.detections_by_run.join('/')
            : String(model.detections || 0);
        const latencyLabel = runCount > 1
            ? `${Number(model.elapsed_ms || 0).toFixed(0)} мс ср.`
            : `${Number(model.elapsed_ms || 0).toFixed(0)} мс`;
        result.textContent = model.ok
            ? `${latencyLabel} · ${detectionsByRun}`
            : 'ОШИБКА';
        if (model.error) item.title = String(model.error);
        item.append(name, result);
        els.frameAnalysisModels.appendChild(item);
    }
}

function renderFrameAnalysisRules(rules) {
    els.frameAnalysisRules.replaceChildren();
    if (!rules.length) {
        appendFrameAnalysisEmpty(
            els.frameAnalysisRules,
            'Ожидание результатов правил',
        );
        return;
    }
    for (const rule of rules) {
        const item = document.createElement('div');
        const stateClass = rule.neutral
            ? ''
            : (rule.skipped
                ? 'skipped'
                : (rule.triggered ? 'triggered' : 'ok'));
        item.className = `frame-analysis-item ${stateClass}`;

        const name = document.createElement('span');
        const result = document.createElement('b');
        name.textContent = rule.name || 'Без названия';
        result.textContent = rule.status_label || (
            rule.skipped
                ? 'НЕ ВЫПОЛНЕНО'
                : (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА')
        );

        item.append(name, result);

        // === НОВАЯ ПРОСТАЯ ПРИЧИНА (human_cause) ===
        if (rule.triggered && rule.human_cause) {
            const cause = document.createElement('div');
            cause.className = 'frame-analysis-human-cause';
            cause.textContent = rule.human_cause;
            item.appendChild(cause);
            item.classList.add('has-human-cause');
        }

        const detailLines = Array.isArray(rule.detail_lines)
            ? rule.detail_lines.filter(Boolean)
            : [];
        const visibleDetails = detailLines.length
            ? detailLines
            : (rule.detail ? [String(rule.detail)] : []);

        // Показываем технические детали только если нет короткой причины
        if (
            (rule.triggered || rule.skipped || rule.show_detail)
            && visibleDetails.length
            && !rule.human_cause
        ) {
            item.classList.add('has-detail');
            for (const detailLine of visibleDetails) {
                const reason = document.createElement('small');
                reason.className = 'frame-analysis-reason';
                reason.textContent = String(detailLine);
                item.appendChild(reason);
            }
        } else if (rule.detail && !rule.human_cause && !rule.triggered) {
            // Для нормальных правил показываем коротко
            if (rule.detail.length < 80) {
                const short = document.createElement('small');
                short.className = 'frame-analysis-reason';
                short.textContent = rule.detail;
                item.appendChild(short);
            }
        }

        els.frameAnalysisRules.appendChild(item);
    }
}

function appendFrameAnalysisEmpty(container, text) {
    const item = document.createElement('div');
    item.className = 'frame-analysis-empty';
    item.textContent = text;
    container.appendChild(item);
}

function showPendingSelectedFrameAnalysis() {
    if (!els.frameAnalysisPanel) return;
    els.frameAnalysisPanel.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-collapsed');
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.add('is-collapsed');
    }
    if (els.statsService) els.statsService.classList.add('is-collapsed');
    setIfChanged(els.frameAnalysisTitle, 'АНАЛИЗ 3 КАДРОВ');
    setIfChanged(els.frameAnalysisContext, cameraRoleLabel(state.currentCamera));
    setIfChanged(els.frameAnalysisMessage, 'Подготовка моделей и правил');
    setIfChanged(els.frameAnalysisModelsTitle, 'МОДЕЛИ');
    setIfChanged(els.frameAnalysisRulesTitle, 'ПРАВИЛА');
    els.frameAnalysisModels.replaceChildren();
    els.frameAnalysisRules.replaceChildren();
    appendFrameAnalysisEmpty(els.frameAnalysisModels, 'Запуск моделей');
    appendFrameAnalysisEmpty(els.frameAnalysisRules, 'Ожидание результатов');
}

function setupSelectedFrameAnalysis() {
    if (!els.analyzeSelectedFrame) return;
    els.analyzeSelectedFrame.addEventListener('click', async () => {
        if (els.analyzeSelectedFrame.disabled || state.selectedAnalysisPending) return;
        state.selectedAnalysisPending = true;
        if (!state.selectedAnalysisActive) {
            showPendingSelectedFrameAnalysis();
        }
        updateViewModeControls();
        els.analyzeSelectedFrame.disabled = true;
        state.backendControls = {
            ...state.backendControls,
            start: false,
            jog_hold: false,
            distributor_diagnostic: false,
            camera_diagnostic: false,
            vision_rule_diagnostic: false,
            selected_model_analysis: false,
        };
        applyButtonsForState(
            state.lineState,
            state.serverExitRequested,
            state.backendControls,
        );
        disablePrestartDiagnosticButtons();
        if (els.distributorDiagnostics) {
            els.distributorDiagnostics.querySelectorAll('button').forEach(
                button => { button.disabled = true; }
            );
        }
        if (els.jogPanel) {
            els.jogPanel.classList.add('is-collapsed');
            els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(
                button => { button.disabled = true; }
            );
        }
        clearControlError();
        try {
            if (state.selectedAnalysisActive) {
                await apiPost('/api/diagnostics/selected/release', true);
            } else {
                const role = state.currentCamera;
                if (!role) return;
                await apiPost(
                    `/api/diagnostics/selected/${encodeURIComponent(role)}`,
                    true,
                );
            }
        } finally {
            state.selectedAnalysisPending = false;
            updateViewModeControls();
            requestImmediateStatus();
        }
    });
}
