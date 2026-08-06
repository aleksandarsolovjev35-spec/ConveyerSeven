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

    // Пороги правил подтягиваются из thresholds.json автоматически: когда
    // backend перечитал файл и увеличил revision, обновляем панель.
    if (typeof status.thresholds_revision === 'number') {
        if (
            state.thresholdsRevision !== null
            && status.thresholds_revision !== state.thresholdsRevision
            && typeof updateThresholdsPanel === 'function'
        ) {
            updateThresholdsPanel(true);
        }
        state.thresholdsRevision = status.thresholds_revision;
    }

    const incomingVersion = (
        typeof status.frame_version === 'number'
            ? status.frame_version
            : null
    );
    // Публикация нового визуального содержимого (кадры, обрисовка правил,
    // цвет корпусов). После изменений в сервере версия растёт только при
    // реально новом содержимом, поэтому это и есть момент «анализ готов».
    const newPublishArrived = (
        incomingVersion !== null
        && incomingVersion !== state.lastSeenVersion
    );

    if (incomingVersion !== null) {
        state.currentVersion = incomingVersion;
        if (
            newPublishArrived
            && state.mainCamMode === 'pull'
            && !state.splashActive
        ) {
            maybeRequestMainFrame();
        }
    }

    // Синхронизация «сначала монитор, потом UI»: кадры с обрисовкой правил
    // уже готовы на сервере, но на экране их ещё нет (их надо запросить и
    // показать). Держим визуальные результаты анализа — цвет корпуса на
    // линии, карточки правил, превью — на предыдущем состоянии и применяем
    // единым кадром, когда кадр с обрисовкой появится на главной камере.
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
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
    updateViewModeControls();
    if (els.jogPanel) {
        els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(button => {
            button.disabled = true;
        });
    }
    if (typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
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

    // Пока ждём кадр с обрисовкой правил (pendingAnalysisVersion), панели
    // анализа и счётчик корпусов держат предыдущий результат — они
    // переключатся вместе с цветом корпуса на линии в flushPendingAnalysis().
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

    // Пороги зависят не только от состояния линии, но и от результата
    // updateSelectedAnalysisStatus/updateJogState выше. Обновляем панель
    // после всех этих полей, иначе во время JOG она могла оставаться
    // визуально доступной один тик статуса.
    if (typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
    }
}

// ─── Line cells — путь деталей по позициям линии ───────────────────────────
// Детали показаны отдельными маркерами поверх статичной сетки из восьми
// ячеек. На каждом шаге линии маркеры плавно сдвигаются на одну позицию
// вправо, поэтому панель наглядно повторяет движение конвейера. Никаких
// эффектов внутри самих ячеек нет: только короткий ID детали в виде «#1».

const _lineTokens = new Map(); // partId -> {el, position, category}
let _lineSyncDone = false;

// Последний применённый (показанный) снимок линии: [{id, position, category}].
// Пока ждём кадр с обрисовкой правил (pendingAnalysisVersion), категории,
// появление новых корпусов и счётчик корпусов берутся из этого снимка:
// линия и камеры обновляются одним кадром («сначала монитор, потом UI»).
let _appliedLineParts = [];
let _appliedInLine = 0;

// Длительность одного шага линии: по ней же движутся маркеры деталей,
// поэтому сдвиг выглядит синхронным с реальным конвейером.
function lineMoveDuration(process = {}) {
    const conv = process.conveyor || {};
    const speed = Number(conv.speed) || 0;
    if (!speed) return 420;
    // 20000 -> 420мс; 30000 -> ~280мс; 15000 -> ~560мс
    return Math.max(265, Math.min(620, Math.round(8400000 / speed)));
}

function _lineCellRects(cells) {
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
    if (outCat === 'BAD') {
        gateOut.classList.add('gate-rejecting');
        gateOut.textContent = '▼ СБРОС';
    } else if (outCat === 'CLEANUP') {
        gateOut.classList.add('gate-cleanup');
        gateOut.textContent = '▼ ОЧИСТКА';
    } else if (isDropPhase && partAtReject) {
        gateOut.classList.add('gate-rejecting');
        gateOut.textContent = '▼ СБРОС';
    } else {
        // Нет детали на сортировке — ворота отражают текущее состояние
        // распределителя: зелёный (production-ready), красный (BAD),
        // жёлтый (CLEANUP) или нейтральный (нет категории).
        if (_currentDistributorCategory === 'BAD') {
            gateOut.classList.add('gate-rejecting');
            gateOut.textContent = 'ВЫХОД ▸';
        } else if (_currentDistributorCategory === 'CLEANUP') {
            gateOut.classList.add('gate-cleanup');
            gateOut.textContent = 'ВЫХОД ▸';
        } else {
            gateOut.classList.add('gate-active');
            gateOut.textContent = 'ВЫХОД ▸';
        }
    }
}

// ─── Заливка распределителя по маршруту детали ──────────────────────
// Вся панель заливается полупрозрачным цветом канала, в который уходит
// деталь: зелёный — годное, красный — брак, жёлтый — очистка. Карточки
// DIST1/DIST2 лежат внутри этой заливки и остаются читаемыми.
//
// Текущая категория распределителя хранится в _currentDistributorCategory
// и используется воротами «ВЫХОД» для синхронизации цвета.

const ROUTE_CATEGORIES = ['GOOD', 'BAD', 'CLEANUP'];
let _currentDistributorCategory = '';   // '', 'GOOD', 'BAD', 'CLEANUP'

function _resolveDistributorRoute(ls) {
    const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
    const process = ls.process || {};
    const phaseText = String(process.phase || '').toUpperCase();
    const routingPhase =
        phaseText.includes('ROUTE') || phaseText.includes('DROP');

    // В фазах маршрутизации бэкенд называет деталь, для которой готовится
    // распределитель: её категория — и есть текущий маршрут.
    let part = null;
    if (routingPhase && process.part_id != null) {
        part = parts.find(
            item => Number(item.id) === Number(process.part_id),
        ) || null;
    }
    // Вне фаз маршрутизации смотрим на деталь, стоящую на сортировке.
    if (!part) {
        part = parts.find(item => Number(item.position) === 7) || null;
    }

    let category = part ? String(part.category || '').toUpperCase() : '';
    if (!ROUTE_CATEGORIES.includes(category)) category = '';

    if (!category) {
        // Без детали маршрут показывает только реально открытая заслонка
        // сброса (в т.ч. ручная проверка СБРОС): цвет — по каналу DIST2.
        // Припаркованный селектор сам по себе панель не заливает.
        const d1State = String(ls.dist1_state || '').toUpperCase();
        // CLOSING — движение к «зелёному» состоянию ПРОХОД: открытой
        // заслонкой оно не считается, и панель сразу подсвечивается
        // зелёным ниже, не дожидаясь фактического прихода заслонки в 0.
        const d1Open = ['OPEN', 'OPENING'].includes(d1State)
            || (d1State !== 'CLOSING' && Number(ls.dist1_position || 0) > 0);
        if (d1Open) {
            category =
                String(ls.dist2_target || '').toUpperCase() === 'CLEANUP'
                    ? 'CLEANUP'
                    : 'BAD';
        }
    }
    return category;
}

function _updateDistributorRoute(ls) {
    const panel = els.distributorDiagnostics;
    if (!panel) return;

    panel.classList.remove(
        'route-good', 'route-bad', 'route-cleanup', 'production-ready',
    );
    const category = _resolveDistributorRoute(ls);
    let effective = '';  // цвет, который реально применён к панели

    if (category === 'GOOD') {
        panel.classList.add('route-good');
        effective = 'GOOD';
    } else if (category === 'BAD') {
        panel.classList.add('route-bad');
        effective = 'BAD';
    } else if (category === 'CLEANUP') {
        panel.classList.add('route-cleanup');
        effective = 'CLEANUP';
    } else {
        // Нет активного маршрута детали: если распределитель в исходном
        // положении (DIST1 закрыт) или уже закрывается к нему, линия не
        // работает — показываем зелёную заливку сразу после команды,
        // а не после фактического прихода заслонки в 0.
        const lineState = (ls.state || state.lineState || '').toUpperCase();
        const d1State = String(ls.dist1_state || '').toUpperCase();
        const closingToHome = d1State === 'CLOSING';
        const parked = ['IDLE', 'STOPPED'].includes(lineState)
            && (d1State === 'IDLE' || closingToHome)
            && (Number(ls.dist1_position || 0) === 0 || closingToHome);
        if (parked) {
            panel.classList.add('production-ready');
            effective = 'GOOD';   // production-ready = зелёный
        }
    }

    _currentDistributorCategory = effective;

    if (els.distRoute) {
        const ready = panel.classList.contains('production-ready');
        const label = category
            ? `→ ${categoryLabel(category)}`
            : (ready ? 'ПРОИЗВОДСТВО ГОТОВО' : '');
        setIfChanged(els.distRoute, label);
    }
}

function updateLineCells(lineParts, process = {}) {
    const isConveyorMoving = (process.phase || '').includes('CONVEYOR') ||
                             (process.phase || '').includes('MOTION') ||
                             (process.phase || '').includes('ROUTE_PREPARE');

    // Спокойная лента под ячейками: без прокрутки насечек, только
    // лёгкая подсветка во время движения.
    let belt = els.lineCells.querySelector('.conveyor-belt');
    if (!belt) {
        belt = document.createElement('div');
        belt.className = 'conveyor-belt';
        els.lineCells.insertBefore(belt, els.lineCells.firstChild);
    }
    belt.classList.toggle('moving', !!isConveyorMoving);

    els.lineCells.style.setProperty(
        '--move-duration', `${lineMoveDuration(process)}ms`,
    );

    const phaseEl = els.processPhaseLabel
        || document.getElementById('process-phase-label');
    if (phaseEl) {
        phaseEl.textContent = process.phase
            ? (process.label || process.phase).slice(0, 64).toUpperCase()
            : '';
        phaseEl.style.opacity = process.phase
            ? (isConveyorMoving ? '0.9' : '0.6')
            : '0';
    }

    // Только реальные ячейки позиций: лента и маркеры деталей не считаются.
    const cells = els.lineCells.querySelectorAll('.line-cell[data-pos]');

    // Подсветка активных на текущем этапе позиций на статичной сетке.
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

    // Чего хотим видеть: partId -> позиция и категория.
    // Синхронизация «сначала монитор, потом UI»: пока кадр с обрисовкой
    // правил не показан на главной камере (pendingAnalysisVersion), корпус
    // на линии сохраняет предыдущую категорию, а новые корпуса не появляются
    // вовсе. Так деталь под камерами и на линии появляется одновременно.
    const pendingAnalysis = state.pendingAnalysisVersion !== null;
    const appliedById = new Map(
        _appliedLineParts.map(part => [part.id, part.category]),
    );
    const wanted = new Map();
    for (const part of lineParts || []) {
        const id = Number(part.id);
        if (pendingAnalysis && !appliedById.has(id)) {
            // Новый корпус этого шага: ждём кадр анализа под камерами.
            continue;
        }
        let category = (part.category || '').toUpperCase();
        if (pendingAnalysis && appliedById.has(id)) {
            category = appliedById.get(id);
        }
        wanted.set(id, {
            position: Math.max(0, Math.min(Number(part.position) || 0, 7)),
            category,
        });
    }

    const {containerRect, rects} = _lineCellRects(cells);
    const geometryReady = !!(
        containerRect.width && rects[0] && rects[0].width
    );
    const step = (rects[0] && rects[1])
        ? (rects[1].left - rects[0].left)
        : (rects[0] ? rects[0].width + 3 : 0);
    const duration = lineMoveDuration(process);

    // Ушедшие с линии детали: съезжают за последнюю ячейку и исчезают.
    for (const [id, token] of [..._lineTokens.entries()]) {
        if (wanted.has(id)) continue;
        _lineTokens.delete(id);
        if (geometryReady && rects[token.position]) {
            token.el.style.left = `${rects[token.position].left + step}px`;
            token.el.style.opacity = '0';
        }
        const el = token.el;
        setTimeout(() => {
            if (el.parentNode) el.parentNode.removeChild(el);
        }, duration + 80);
    }

    if (!geometryReady) {
        // Панель ещё свёрнута: позиции маркеров выставим на следующем тике.
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
                // Новая деталь въезжает на вход вместе со сдвигом линии.
                el.style.left = `${target.left - step}px`;
                el.style.opacity = '0';
                requestAnimationFrame(() => {
                    el.style.left = `${target.left}px`;
                    el.style.opacity = '1';
                });
            } else {
                // Первая отрисовка (запуск/перезагрузка UI): ставим маркеры
                // сразу на свои ячейки без движения.
                el.style.left = `${target.left}px`;
                el.style.opacity = '1';
            }
        } else {
            // Геометрия сверяется каждый тик: ресайз окна двигает маркеры
            // вместе с ячейками, а смена позиции на шаге конвейера
            // анимируется CSS-переходом left.
            token.el.style.top = `${target.top}px`;
            token.el.style.width = `${target.width}px`;
            token.el.style.height = `${target.height}px`;
            const targetLeft = `${target.left}px`;
            if (token.el.style.left !== targetLeft) {
                token.el.style.left = targetLeft;
            }
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
            position: Math.max(
                0,
                Math.min(Number(part.position) || 0, 7),
            ),
            category: (part.category || '').toUpperCase(),
        }));
        _appliedInLine = _appliedLineParts.length;
    }

    _updateLineGates(lineParts, process);
    _lineSyncDone = true;
}

// ─── Синхронизация «сначала монитор, потом UI» ─────────────────────────
// Публикация анализа приходит в /api/status мгновенно, а кадр с обрисовкой
// правил — отдельным запросом /frame. Чтобы цвет корпуса на линии не менялся
// раньше обрисовки под камерами, визуальные результаты анализа (цвет
// корпусов, карточки правил, превью) удерживаются до показа кадра на
// главной камере и применяются одним махом в flushPendingAnalysis().

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
    if (state.pendingFlushTimer) {
        clearTimeout(state.pendingFlushTimer);
        state.pendingFlushTimer = null;
    }
    // Кадр с обрисовкой уже показан: применяем самый свежий снимок статуса.
    const ls = state.lastLineStatus;
    if (ls) {
        updateLineCells(ls.line_parts || [], ls.process || {});
        if (typeof updateNewFrameAnalysisStatus === 'function') {
            updateNewFrameAnalysisStatus(ls);
        }
    }
    if (typeof refreshPreviewStrip === 'function') {
        refreshPreviewStrip();
    }
}
