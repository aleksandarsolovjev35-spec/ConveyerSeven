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

// ─── Line cells — реалистичная анимация движения деталей по линии ───────────────────────────

let _prevLineParts = [];

// Длительность одного шага линии: анимации деталей и прокрутка ленты
// используют одно и то же значение, поэтому движение выглядит синхронным.
function lineMoveDuration(process = {}) {
    const conv = process.conveyor || {};
    const speed = Number(conv.speed) || 0;
    if (!speed) return 420;
    // 20000 -> 420мс; 30000 -> ~280мс; 15000 -> ~560мс
    return Math.max(265, Math.min(620, Math.round(8400000 / speed)));
}

function updateLineCells(lineParts, process = {}) {
    const isConveyorMoving = (process.phase || '').includes('CONVEYOR') ||
                             (process.phase || '').includes('MOTION') ||
                             (process.phase || '').includes('ROUTE_PREPARE');

    // Анимация ленты (реальное движение)
    let belt = els.lineCells.querySelector('.conveyor-belt');
    if (!belt) {
        belt = document.createElement('div');
        belt.className = 'conveyor-belt';
        const track = document.createElement('div');
        track.className = 'conveyor-belt-track';
        // Насечки ленты — реальные элементы, чтобы движение было видимым.
        for (let n = 0; n < 80; n++) {
            const notch = document.createElement('span');
            notch.className = 'conveyor-notch';
            track.appendChild(notch);
        }
        belt.appendChild(track);
        els.lineCells.insertBefore(belt, els.lineCells.firstChild);
    }
    belt.classList.toggle('moving', !!isConveyorMoving);

    // Длительность шага ленты = длительность анимаций деталей (синхронно).
    els.lineCells.style.setProperty('--move-duration', `${lineMoveDuration(process)}ms`);

    // Только реальные ячейки позиций: лента и "летящие" копии не считаются.
    const cells = els.lineCells.querySelectorAll('.line-cell[data-pos]');

    const phaseEl = document.getElementById('process-phase') || (() => {
        const p = document.createElement('div');
        p.id = 'process-phase';
        p.style.cssText = 'position:absolute;right:6px;top:-1px;font:700 8.5px var(--font-mono);color:#8fa3b8;opacity:0.85;pointer-events:none;';
        els.lineCells.parentElement.appendChild(p);
        return p;
    })();
    if (process.phase) {
        phaseEl.textContent = (process.label || process.phase).slice(0, 22).toUpperCase();
        phaseEl.style.opacity = isConveyorMoving ? '0.95' : '0.65';
    } else {
        phaseEl.textContent = '';
    }

    const current = lineParts || [];
    const prev = _prevLineParts || [];
    const doAnimate = isConveyorMoving;

    // Сброс стилей
    for (let i = 0; i < cells.length; i++) {
        const c = cells[i];
        c.style.transitionDuration = '';
        c.style.transform = '';
        c.style.opacity = '';
        c.classList.remove('moving-right', 'entering', 'exiting');
    }

    // Обновляем содержимое
    for (let i = 0; i < cells.length; i++) {
        const cell = cells[i];

        const position = Number(cell.dataset.pos);
        const part = current.find(p => p.position === position);
        cell.className = 'line-cell';
        cell.dataset.partId = part ? part.id : '';

        if (part) {
            cell.textContent = `№${part.id}`;
            cell.classList.add('occupied');
            const cat = (part.category || '').toUpperCase();
            if (cat === 'BAD') cell.classList.add('cell-bad');
            else if (cat === 'CLEANUP') cell.classList.add('cell-cleanup');
            else if (cat === 'GOOD') cell.classList.add('cell-good');
            cell.title = `Деталь №${part.id} · ${categoryLabel(part.category)}`;
        } else {
            cell.textContent = '';
            cell.title = '';
        }

        const active = Array.isArray(process.positions) ? process.positions : [];
        if (active.includes(position)) {
            cell.classList.add('process-active');
            if ((process.phase || '').includes('CAMERA') || (process.phase || '').includes('ANALYSIS')) cell.classList.add('process-camera');
            if ((process.phase || '').includes('ROUTE') || (process.phase || '').includes('DROP')) cell.classList.add('process-route');
        }
    }

    // === РЕАЛИСТИЧНЫЕ АНИМАЦИИ ДВИЖЕНИЯ (только когда лента реально едет) ===
    // Под реальную механику: 1 шаг = ~410-450мс (move_step + wait_stop)
    // Используем conveyor.speed для масштабирования (выше скорость — короче анимация)
    if (doAnimate) {
        const DURATION = lineMoveDuration(process);

        // 1. Реальное перемещение деталей: используем "летающие" overlay-элементы
        //    + улучшенный реализм (подъём + микро-вибрация)
        for (let i = 0; i < 7; i++) {
            const fromCell = cells[i];
            const had = prev.find(p => p.position === i);
            const has = current.find(p => p.position === i);

            const movedRight = had && (!has || has.id !== had.id);

            if (movedRight) {
                const toCell = cells[i + 1];
                if (!toCell || !fromCell) continue;

                const partId = had.id;
                const cat = (had.category || '').toUpperCase();

                const flyer = document.createElement('div');
                flyer.className = 'line-cell flying-part lifting wobbling';
                flyer.textContent = `№${partId}`;
                flyer.dataset.partId = partId;

                if (cat === 'BAD') flyer.classList.add('cell-bad');
                else if (cat === 'CLEANUP') flyer.classList.add('cell-cleanup');
                else if (cat === 'GOOD') flyer.classList.add('cell-good');

                const parentRect = els.lineCells.getBoundingClientRect();
                const fromRect = fromCell.getBoundingClientRect();
                const toRect = toCell.getBoundingClientRect();

                const startLeft = fromRect.left - parentRect.left;
                const startTop = fromRect.top - parentRect.top;

                flyer.style.position = 'absolute';
                flyer.style.left = `${startLeft}px`;
                flyer.style.top = `${startTop}px`;
                flyer.style.width = `${fromRect.width}px`;
                flyer.style.height = `${fromRect.height}px`;
                flyer.style.transition = `transform ${DURATION}ms cubic-bezier(0.18, 0.0, 0.22, 1), opacity ${DURATION}ms ease`;
                flyer.style.zIndex = '45';
                flyer.style.boxShadow = '1px 6px 14px rgba(0,0,0,0.42)';

                // Начальное положение с подъёмом
                flyer.style.transform = 'translateY(-2.5px) scale(1.02)';

                els.lineCells.appendChild(flyer);

                requestAnimationFrame(() => {
                    const deltaX = toRect.left - fromRect.left;

                    // Финальная позиция: подъём сохраняется, потом в конце будет проседание
                    flyer.style.transform = `translateX(${deltaX}px) translateY(-2px) scale(1.015)`;

                    // Проседание в конце движения (реалистичный "приземление" на ленте)
                    setTimeout(() => {
                        if (flyer && flyer.parentNode) {
                            flyer.style.transition = `transform ${Math.round(DURATION * 0.28)}ms cubic-bezier(0.4, 0.0, 1, 1)`;
                            flyer.style.transform = `translateX(${deltaX}px) translateY(0px) scale(0.995)`;
                        }
                    }, DURATION * 0.72);

                    setTimeout(() => {
                        if (flyer && flyer.parentNode) {
                            flyer.parentNode.removeChild(flyer);
                        }
                    }, DURATION + 45);
                });

                // Очищаем исходную ячейку
                setTimeout(() => {
                    if (fromCell && fromCell.parentNode && !current.find(p => p.position === i)) {
                        fromCell.textContent = '';
                        fromCell.classList.remove('occupied', 'cell-bad', 'cell-cleanup', 'cell-good');
                    }
                }, DURATION * 0.62);
            }
        }

        // 2. Появление новой детали на входе (позиция 0) — реалистичный въезд
        const newIn = current.find(p => p.position === 0);
        const oldIn = prev.find(p => p.position === 0);
        const c0 = cells[0];

        if (newIn && (!oldIn || oldIn.id !== newIn.id)) {
            const cat = (newIn.category || '').toUpperCase();
            c0.textContent = `№${newIn.id}`;
            c0.className = 'line-cell occupied entering-with-trail';
            if (cat === 'BAD') c0.classList.add('cell-bad');
            else if (cat === 'CLEANUP') c0.classList.add('cell-cleanup');
            else if (cat === 'GOOD') c0.classList.add('cell-good');

            // Начальное состояние: сильно слева + чуть сжата + почти невидима
            c0.style.transitionDuration = '0ms';
            c0.style.transform = 'translateX(-122%) scale(0.82)';
            c0.style.opacity = '0.04';

            requestAnimationFrame(() => {
                c0.style.transitionDuration = `${DURATION}ms`;
                c0.style.transitionTimingFunction = 'cubic-bezier(0.18, 0.0, 0.22, 1)';

                // Въезд + небольшое "распрямление"
                c0.style.transform = 'translateX(0) scale(1)';
                c0.style.opacity = '1';

                // Убираем класс следа после въезда
                setTimeout(() => {
                    if (c0 && c0.parentNode) {
                        c0.classList.remove('entering-with-trail');
                        c0.style.transitionDuration = '';
                        c0.style.transform = '';
                        c0.style.opacity = '';
                    }
                }, DURATION + 40);
            });
        }

        // 3. Уход детали в распределитель (позиция 7) — драматичный вылет + падение
        const oldOut = prev.find(p => p.position === 7);
        const newOut = current.find(p => p.position === 7);
        const c7 = cells[7];

        if (oldOut && (!newOut || newOut.id !== oldOut.id)) {
            const partId = oldOut.id;
            const cat = (oldOut.category || '').toUpperCase();

            // Создаём "падающую" копию детали — вылетает вправо и вниз (в распределитель)
            const exitFlyer = document.createElement('div');
            exitFlyer.className = 'line-cell flying-part exiting-flyer';
            exitFlyer.textContent = `№${partId}`;
            exitFlyer.dataset.partId = partId;

            if (cat === 'BAD') exitFlyer.classList.add('cell-bad');
            else if (cat === 'CLEANUP') exitFlyer.classList.add('cell-cleanup');
            else if (cat === 'GOOD') exitFlyer.classList.add('cell-good');

            const parentRect = els.lineCells.getBoundingClientRect();
            const c7Rect = c7.getBoundingClientRect();

            const startLeft = c7Rect.left - parentRect.left;
            const startTop = c7Rect.top - parentRect.top;

            exitFlyer.style.position = 'absolute';
            exitFlyer.style.left = `${startLeft}px`;
            exitFlyer.style.top = `${startTop}px`;
            exitFlyer.style.width = `${c7Rect.width}px`;
            exitFlyer.style.height = `${c7Rect.height}px`;
            exitFlyer.style.zIndex = '50';
            exitFlyer.style.transition = `transform ${DURATION}ms cubic-bezier(0.32, 0.02, 0.55, 1), 
                                          opacity ${DURATION}ms ease`;
            exitFlyer.style.boxShadow = '2px 8px 18px rgba(0,0,0,0.45)';

            els.lineCells.appendChild(exitFlyer);

            // Прячем оригинальную ячейку сразу
            c7.style.transitionDuration = '60ms';
            c7.style.opacity = '0.15';

            requestAnimationFrame(() => {
                // Драматичный вылет: вправо + вниз + наклон + уменьшение
                exitFlyer.style.transform = `translateX(138px) translateY(38px) rotate(14deg) scale(0.74)`;
                exitFlyer.style.opacity = '0';

                // Пульс лопастей распределителя (если есть)
                pulseDistributorBlades(DURATION);
            });

            setTimeout(() => {
                if (exitFlyer && exitFlyer.parentNode) {
                    exitFlyer.parentNode.removeChild(exitFlyer);
                }
                if (c7 && c7.parentNode) {
                    if (!newOut) {
                        c7.textContent = '';
                        c7.classList.remove('occupied', 'cell-bad', 'cell-cleanup', 'cell-good');
                    }
                    c7.style.transitionDuration = '';
                    c7.style.opacity = '';
                    c7.style.transform = '';
                }
            }, DURATION + 60);
        }
    }

    _prevLineParts = current.map(p => ({...p}));
}

// === Вспомогательная функция: пульс лопастей распределителя при вылете детали ===
function pulseDistributorBlades(duration = 420) {
    const blades = [
        document.getElementById('dist1-blade'),
        document.getElementById('dist2-blade')
    ].filter(Boolean);

    blades.forEach(blade => {
        const origTransition = blade.style.transition;
        blade.style.transition = `box-shadow ${duration * 0.4}ms ease, transform ${duration * 0.35}ms ease`;

        // Яркая вспышка + лёгкий "толчок"
        blade.style.boxShadow = '0 0 0 4px rgba(255, 200, 80, 0.55)';
        blade.style.transform = 'translate(-50%, -50%) scale(1.15)';

        setTimeout(() => {
            if (blade) {
                blade.style.boxShadow = '';
                blade.style.transform = 'translate(-50%, -50%)';
            }
        }, Math.round(duration * 0.55));

        setTimeout(() => {
            if (blade) {
                blade.style.transition = origTransition || 'left 100ms linear';
            }
        }, duration + 80);
    });
}
