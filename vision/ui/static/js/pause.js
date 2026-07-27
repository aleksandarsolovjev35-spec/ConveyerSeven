// pause.js — Line Monitor UI module
'use strict';

// ─── Пауза внутри цикла и ограниченная коррекция ленты ───────

function updatePauseState(status) {
    const pause = (status && status.pause) || {};
    const controls = state.backendControls || {};

    state.pauseActive     = !!pause.active;
    state.pauseRequested  = !!pause.requested;
    state.nudgeOffset     = Number(pause.nudge_offset) || 0;
    state.nudgeLimitSteps = Number(pause.nudge_limit_steps) || 0;
    state.nudgeHoldBusy   = !!pause.hold_busy;

    if (!state.pauseActive && (
        state.nudgeHoldDirection || state.nudgeHoldStartPromise
    )) {
        releaseNudgeHoldBestEffort('pause ended');
    }

    showNudgePanel(state.pauseActive);
    renderNudgeReadout(pause);

    const blocked = (
        state.offline
        || state.controlPending
        || state.nudgeHoldPending
        || controls.nudge_hold !== true
    );

    applyNudgeButton(
        els.nudgeForward,
        '+',
        blocked || Number(pause.remaining_forward) <= 0,
        pause,
    );
    applyNudgeButton(
        els.nudgeBackward,
        '-',
        blocked || Number(pause.remaining_backward) <= 0,
        pause,
    );

    if (pause.hold_busy && state.nudgeHoldDirection) {
        state.nudgeHoldConfirmed = true;
    }

    // Локальное удержание снимается только после того, как сервер сначала
    // подтвердил движение, а затем сообщил об остановке. Иначе снимок
    // статуса, собранный до старта, оборвал бы heartbeat под пальцем.
    if (
        !pause.hold_busy
        && state.nudgeHoldDirection
        && state.nudgeHoldConfirmed
        && !state.nudgeHoldReleasePending
        && !state.nudgeHoldStartPromise
    ) {
        clearNudgeHoldLocalState();
    }
}

function applyNudgeButton(button, direction, budgetBlocked, pause) {
    if (!button) return;
    const holding = state.nudgeHoldDirection === direction;
    // Кнопку удержания нельзя гасить под пальцем оператора: пока идёт
    // её собственное движение, она обязана остаться активной, иначе
    // pointerup не дойдёт и лента продолжит ход.
    button.disabled = holding ? false : (
        budgetBlocked
        || (!!pause.hold_busy && pause.hold_direction !== direction)
    );
    button.classList.toggle(
        'nudge-active',
        !!pause.hold_busy && pause.hold_direction === direction,
    );
}

function showNudgePanel(visible) {
    if (!els.nudgePanel) return;
    els.nudgePanel.classList.toggle('is-collapsed', !visible);
    els.nudgePanel.classList.toggle('is-holding', visible && state.nudgeHoldBusy);
    if (!visible) {
        els.nudgePanel.querySelectorAll('.nudge-btn').forEach(button => {
            button.disabled = true;
        });
    }
}

function renderNudgeReadout(pause) {
    const limit = Number(pause.nudge_limit_steps) || 0;
    const offset = Number(pause.nudge_offset) || 0;

    if (els.nudgeOffset) {
        const text = offset > 0 ? `+${offset}` : String(offset);
        setIfChanged(els.nudgeOffset, text);
        els.nudgeOffset.classList.toggle('is-limit', limit > 0 && Math.abs(offset) >= limit);
    }
    if (els.nudgeLimit) {
        setIfChanged(els.nudgeLimit, limit ? `ПРЕДЕЛ ±${limit}` : '—');
    }
    if (els.nudgeBarFill && limit > 0) {
        // Полоса заполняется от центра: влево при минусе, вправо при плюсе.
        const ratio = Math.max(-1, Math.min(1, offset / limit));
        const halfWidth = Math.abs(ratio) * 50;
        const left = ratio >= 0 ? 50 : 50 - halfWidth;
        els.nudgeBarFill.style.left = `${left}%`;
        els.nudgeBarFill.style.width = `${halfWidth}%`;
        els.nudgeBarFill.classList.toggle('is-limit', Math.abs(ratio) >= 1);
    } else if (els.nudgeBarFill) {
        els.nudgeBarFill.style.width = '0%';
        els.nudgeBarFill.style.left = '50%';
    }
}

async function beginNudgeHold(direction, button) {
    if (
        !state.pauseActive
        || state.offline
        || state.controlPending
        || state.nudgeHoldPending
        || state.backendControls.nudge_hold !== true
    ) return;
    if (direction !== '+' && direction !== '-') return;
    if (state.nudgeHoldDirection) return;

    state.nudgeHoldDirection = direction;
    state.nudgeHoldConfirmed = false;
    state.nudgeHoldReleasePending = false;
    button.classList.add('nudge-active');
    clearControlError();

    state.nudgeHoldStartPromise = apiPostJson(
        '/api/nudge/hold/start',
        {direction},
        true,
    );
    const result = await state.nudgeHoldStartPromise;
    state.nudgeHoldStartPromise = null;

    if (!result) {
        clearNudgeHoldLocalState();
        requestImmediateStatus();
        return;
    }
    if (state.nudgeHoldReleasePending || state.nudgeHoldDirection !== direction) {
        await releaseNudgeHold('released during start');
        return;
    }
    const heartbeat = await apiPostJson('/api/nudge/hold/heartbeat', {
        direction,
    });
    if (!heartbeat) {
        await releaseNudgeHold('initial heartbeat rejected');
        return;
    }
    startNudgeHeartbeat(direction);
    requestImmediateStatus();
}

function startNudgeHeartbeat(direction) {
    stopNudgeHeartbeat();
    state.nudgeHeartbeatTimer = setInterval(async () => {
        if (
            state.nudgeHoldDirection !== direction
            || state.nudgeHeartbeatBusy
        ) return;
        state.nudgeHeartbeatBusy = true;
        const result = await apiPostJson('/api/nudge/hold/heartbeat', {
            direction,
        });
        state.nudgeHeartbeatBusy = false;
        if (!result && state.nudgeHoldDirection === direction) {
            releaseNudgeHold('heartbeat rejected');
        }
    }, JOG_HEARTBEAT_INTERVAL);
}

function stopNudgeHeartbeat() {
    if (state.nudgeHeartbeatTimer) {
        clearInterval(state.nudgeHeartbeatTimer);
        state.nudgeHeartbeatTimer = null;
    }
}

async function releaseNudgeHold(reason = 'button released') {
    if (!state.nudgeHoldDirection && !state.nudgeHoldStartPromise) return;
    if (state.nudgeHoldReleasePending) return;
    state.nudgeHoldReleasePending = true;
    stopNudgeHeartbeat();
    if (els.nudgePanel) {
        els.nudgePanel.querySelectorAll('.nudge-btn').forEach(button => {
            button.classList.remove('nudge-active');
        });
    }

    if (state.nudgeHoldStartPromise) {
        await state.nudgeHoldStartPromise;
        state.nudgeHoldStartPromise = null;
    }
    await apiPostJson('/api/nudge/hold/release', {reason}, true);
    clearNudgeHoldLocalState();
    requestImmediateStatus();
}

function releaseNudgeHoldBestEffort(reason) {
    if (!state.nudgeHoldDirection && !state.nudgeHoldStartPromise) return;
    stopNudgeHeartbeat();
    state.nudgeHoldReleasePending = true;
    fetch('/api/nudge/hold/release', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
        keepalive: true,
    }).catch(() => {});
    clearNudgeHoldLocalState();
}

function clearNudgeHoldLocalState() {
    stopNudgeHeartbeat();
    state.nudgeHoldDirection = null;
    state.nudgeHoldConfirmed = false;
    state.nudgeHeartbeatBusy = false;
    state.nudgeHoldReleasePending = false;
    state.nudgeHoldStartPromise = null;
    if (els.nudgePanel) {
        els.nudgePanel.querySelectorAll('.nudge-btn').forEach(button => {
            button.classList.remove('nudge-active');
        });
    }
}

function setupPauseControls() {
    if (els.btnPause) {
        els.btnPause.addEventListener('click', async () => {
            if (els.btnPause.classList.contains('is-hidden')
                || els.btnPause.disabled) return;
            flashButton(els.btnPause);
            await submitControl('/api/pause');
        });
    }

    if (els.btnResume) {
        els.btnResume.addEventListener('click', async () => {
            if (els.btnResume.classList.contains('is-hidden')
                || els.btnResume.disabled) return;
            flashButton(els.btnResume);
            await submitControl('/api/resume');
        });
    }

    if (!els.nudgePanel) return;

    els.nudgePanel.querySelectorAll('.nudge-btn').forEach(button => {
        const direction = button.dataset.direction === 'forward' ? '+' : '-';
        button.addEventListener('contextmenu', event => event.preventDefault());
        button.addEventListener('pointerdown', event => {
            event.preventDefault();
            if (button.disabled) return;
            if (button.setPointerCapture) {
                try { button.setPointerCapture(event.pointerId); } catch (_) {}
            }
            beginNudgeHold(direction, button);
        });
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
            button.addEventListener(name, () => {
                releaseNudgeHold(`UI ${name}`);
            });
        }
        button.addEventListener('pointerleave', event => {
            if (event.buttons === 0 || state.nudgeHoldDirection) {
                releaseNudgeHold('pointer left button');
            }
        });
    });

    window.addEventListener('blur', () => releaseNudgeHold('window blur'));
    window.addEventListener('pagehide', () => {
        releaseNudgeHoldBestEffort('page hidden');
    });
    window.addEventListener('beforeunload', () => {
        releaseNudgeHoldBestEffort('page unload');
    });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) releaseNudgeHoldBestEffort('document hidden');
    });
}
