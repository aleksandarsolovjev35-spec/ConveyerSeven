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

    showNudgePanel(state.pauseActive);
    renderNudgeReadout(pause);

    const blocked = (
        state.offline
        || state.controlPending
        || state.nudgePending
        || controls.nudge !== true
    );

    if (els.nudgeForward) {
        els.nudgeForward.disabled = (
            blocked || Number(pause.remaining_forward) <= 0
        );
    }
    if (els.nudgeBackward) {
        els.nudgeBackward.disabled = (
            blocked || Number(pause.remaining_backward) <= 0
        );
    }
}

function showNudgePanel(visible) {
    if (!els.nudgePanel) return;
    els.nudgePanel.classList.toggle('is-collapsed', !visible);
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

async function submitNudge(direction) {
    if (state.nudgePending || state.offline || state.controlPending) return;
    if (!state.pauseActive) return;

    state.nudgePending = true;
    if (els.nudgeForward)  els.nudgeForward.disabled = true;
    if (els.nudgeBackward) els.nudgeBackward.disabled = true;
    try {
        clearControlError();
        await apiPost(`/api/nudge/${direction}`, true);
    } finally {
        state.nudgePending = false;
        requestImmediateStatus();
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

    if (els.nudgeForward) {
        els.nudgeForward.addEventListener('click', () => {
            if (els.nudgeForward.disabled) return;
            submitNudge('forward');
        });
    }
    if (els.nudgeBackward) {
        els.nudgeBackward.addEventListener('click', () => {
            if (els.nudgeBackward.disabled) return;
            submitNudge('backward');
        });
    }
}
