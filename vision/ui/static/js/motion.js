// motion.js — Line Monitor UI module
'use strict';

// ─── Привод анимаций интерфейса ──────────────────────────────
//
// Правило модуля: в интерфейсе не существует «декоративного» движения.
// Любая анимация — это отображение реальной работы конвейера:
//   • ход ленты в рабочем цикле (фаза CONVEYOR_* и счётчик I2 POS/TGT);
//   • ручное движение ленты (JOG-удержание);
//   • ограниченная коррекция ленты в паузе (накопитель микрошагов);
//   • перемещение заслонок распределителя;
//   • съёмка камер, анализ детали и сортировка на текущем шаге.
//
// Телеметрия превращается в CSS-переменные и классы состояния.
// Когда лента стоит — переменные не меняются, и интерфейс замирает.

const BELT_TOOTH_PITCH_PX       = 18;
const BELT_JOG_RATE_CELLS       = 0.55;
const BELT_NUDGE_STEPS_PER_CELL = 4000;
const BELT_CATCHUP_CELLS_PER_S  = 6.0;
const BELT_CATCHUP_TAU_S        = 0.085;
const BELT_RESYNC_CELLS         = 1.5;
const BELT_STOP_EPS             = 0.0015;
const BELT_CADENCE_MIN_S        = 0.45;
const BELT_CADENCE_MAX_S        = 6.0;
const BELT_FRESH_PART_MS        = 1100;
const BELT_STEP_TICK_MS         = 520;
const BELT_LANE_REBUILD_MS      = 900;
const BELT_MAX_FRAME_DT_S       = 0.1;

const belt = {
    phase:            0,      // пройденные ячейки линии (знаковое число)
    target:           0,      // куда лента доехала по данным контроллера
    velocity:         0,      // ячеек в секунду в текущем кадре
    intra:            0,      // доля текущего шага, 0..1
    progress:         0,
    direction:        1,
    source:           'IDLE', // CYCLE | JOG | NUDGE | IDLE
    active:           false,
    running:          false,
    step:             0,
    stepAt:           0,
    cadence:          1.6,    // измеренная длительность шага, с
    cellPx:           0,
    holdActive:       false,
    lastFrameAt:      0,
    lastNudgeOffset:  null,
    lastLaneBuildAt:  0,
    lastReadout:      '',
    knownParts:       new Set(),
    freshParts:       new Map(),
    started:          false,
    frameHandle:      null,
};

state.belt = belt;

const BELT_READOUT_LABELS = {
    JOG:   'ПРИВОД · РУЧНОЙ ХОД',
    NUDGE: 'ПРИВОД · КОРРЕКЦИЯ',
};

// ─── Мелкие помощники ────────────────────────────────────────

function beltNow() {
    if (typeof performance !== 'undefined' && performance.now) {
        return performance.now();
    }
    return Date.now();
}

function beltClamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function beltMod(value, period) {
    if (!period) return 0;
    return ((value % period) + period) % period;
}

function beltReducedMotion() {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    try {
        return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (_) {
        return false;
    }
}

function pulseClass(el, className, duration = BELT_STEP_TICK_MS) {
    if (!el || beltReducedMotion()) return;
    el.classList.remove(className);
    void el.offsetWidth;
    el.classList.add(className);
    setTimeout(() => el.classList.remove(className), duration);
}

function setBeltVar(name, value) {
    document.documentElement.style.setProperty(name, value);
}

// ─── Зубчатые ленты (общий примитив движения) ────────────────

function refreshBeltLanes(force = false) {
    const now = beltNow();
    if (!force && now - belt.lastLaneBuildAt < BELT_LANE_REBUILD_MS) return;
    belt.lastLaneBuildAt = now;

    document.querySelectorAll('[data-belt-lane]').forEach(lane => {
        const width = lane.clientWidth || lane.offsetWidth || 0;
        const needed = Math.max(
            6,
            Math.ceil(width / BELT_TOOTH_PITCH_PX) + 3,
        );
        let strip = lane.querySelector('.belt-lane-strip');
        if (!strip) {
            strip = document.createElement('div');
            strip.className = 'belt-lane-strip';
            lane.appendChild(strip);
        }
        while (strip.childElementCount < needed) {
            const tooth = document.createElement('span');
            tooth.className = 'belt-tooth';
            strip.appendChild(tooth);
        }
        while (strip.childElementCount > needed) {
            strip.removeChild(strip.lastChild);
        }
    });
}

function measureBeltCell() {
    const cells = els.lineCells ? els.lineCells.children : null;
    if (!cells || cells.length < 2) return;
    const px = Math.abs(cells[1].offsetLeft - cells[0].offsetLeft);
    if (px > 0 && px !== belt.cellPx) {
        belt.cellPx = px;
        setBeltVar('--belt-cell-px', `${px}px`);
    }
}

// ─── Телеметрия ленты ────────────────────────────────────────

function beltConveyorProgress(process) {
    const conveyor = (process && process.conveyor) || {};
    const target = Number(
        conveyor.tgt ?? conveyor.target ?? conveyor.target_steps ?? 0,
    );
    const position = Number(
        conveyor.pos ?? conveyor.position ?? conveyor.current ?? 0,
    );
    if (!Number.isFinite(target) || Math.abs(target) < 1) {
        return {progress: 0, direction: 1};
    }
    return {
        progress: beltClamp(Math.abs(position) / Math.abs(target), 0, 1),
        direction: target < 0 ? -1 : 1,
    };
}

function noteBeltStep(step) {
    const now = beltNow();
    if (belt.stepAt > 0) {
        belt.cadence = beltClamp(
            (now - belt.stepAt) / 1000,
            BELT_CADENCE_MIN_S,
            BELT_CADENCE_MAX_S,
        );
    }
    belt.stepAt = now;
    belt.step = step;
    belt.target = step;
    pulseClass(els.stateIndicator, 'step-pulse', BELT_STEP_TICK_MS);
    pulseClass(els.metricStep, 'num-roll', 300);
    pulseClass(els.lineCells, 'is-settling', BELT_STEP_TICK_MS);
}

function noteBeltParts(lineParts) {
    const now = beltNow();
    const present = new Set();

    for (const part of lineParts) {
        const id = part && part.id;
        if (id === undefined || id === null) continue;
        present.add(id);
        if (!belt.knownParts.has(id)) {
            belt.freshParts.set(id, now);
            beltCellEffect(0, 'is-arriving');
        }
    }
    for (const id of belt.knownParts) {
        if (!present.has(id)) {
            beltCellEffect(7, 'is-dropping');
        }
    }
    belt.knownParts = present;

    for (const [id, at] of belt.freshParts) {
        if (now - at > BELT_FRESH_PART_MS) belt.freshParts.delete(id);
    }
}

function beltCellEffect(index, className) {
    const cells = els.lineCells ? els.lineCells.children : null;
    if (!cells || !cells[index]) return;
    const fx = cells[index].querySelector('.line-cell-fx');
    pulseClass(fx, className, BELT_STEP_TICK_MS);
}

function beltFreshPart(id) {
    return belt.freshParts.has(id);
}

function beltFreshPartClass(id) {
    return beltFreshPart(id) ? ' is-fresh' : '';
}

function updateBeltTelemetry(ls) {
    const status = ls || {};
    const process = status.process || {};
    const phaseName = String(process.phase || '').toUpperCase();
    const jog = status.jog || {};
    const pause = status.pause || {};
    const step = Number(status.step) || 0;
    const conveyor = beltConveyorProgress(process);

    if (step !== belt.step) noteBeltStep(step);

    const cycleMoving = phaseName.includes('CONVEYOR');
    const jogMoving = !!(jog.busy && jog.direction);
    const nudgeMoving = !!(pause.hold_busy && pause.hold_direction);

    const nudgeOffset = Number(pause.nudge_offset);
    const hasNudgeOffset = Number.isFinite(nudgeOffset);

    if (cycleMoving) {
        belt.source = 'CYCLE';
        belt.direction = conveyor.direction;
        belt.progress = conveyor.progress;
        belt.target = step + conveyor.direction * conveyor.progress;
        belt.holdActive = false;
    } else if (jogMoving) {
        belt.source = 'JOG';
        belt.direction = jog.direction === '-' ? -1 : 1;
        belt.progress = 0;
        belt.holdActive = true;
    } else if (nudgeMoving || (hasNudgeOffset && belt.source === 'NUDGE')) {
        belt.source = 'NUDGE';
        belt.direction = pause.hold_direction === '-' ? -1 : 1;
        belt.progress = 0;
        belt.holdActive = false;
    } else {
        belt.source = 'IDLE';
        belt.progress = 0;
        belt.holdActive = false;
    }

    // Коррекция в паузе двигает ленту ровно на измеренные микрошаги.
    if (hasNudgeOffset) {
        if (belt.lastNudgeOffset === null) {
            belt.lastNudgeOffset = nudgeOffset;
        } else if (nudgeOffset !== belt.lastNudgeOffset) {
            const delta = nudgeOffset - belt.lastNudgeOffset;
            belt.lastNudgeOffset = nudgeOffset;
            belt.target += delta / BELT_NUDGE_STEPS_PER_CELL;
            belt.source = 'NUDGE';
            belt.direction = delta < 0 ? -1 : 1;
        }
    } else {
        belt.lastNudgeOffset = null;
    }

    belt.active = cycleMoving || jogMoving || nudgeMoving;

    updateWorkPhaseClasses(status, phaseName);
    updateDistributorMotion(status);
    noteBeltParts(Array.isArray(status.line_parts) ? status.line_parts : []);
    measureBeltCell();
    updateBeltReadout();
    startBeltDrive();
}

function freezeBeltMotion() {
    belt.source = 'IDLE';
    belt.active = false;
    belt.holdActive = false;
    belt.target = belt.phase;
    belt.velocity = 0;
    belt.lastNudgeOffset = null;
    updateWorkPhaseClasses({}, '');
    writeBeltVars();
}

// ─── Работа механизмов на текущем шаге ───────────────────────

function updateWorkPhaseClasses(status, phaseName) {
    const container = els.cameraContainer
        || document.querySelector('.camera-container');
    const working = ['RUNNING', 'STOPPING'].includes(state.lineState);

    if (document.body) {
        document.body.classList.toggle('line-working', working);
        document.body.classList.toggle('line-paused', state.lineState === 'PAUSED');
    }
    if (!container) return;

    const capturing = phaseName.includes('CAMERA')
        || phaseName.includes('CAPTURE');
    const analysing = phaseName.includes('ANALYSIS')
        || phaseName.includes('SPIDER_CHECK')
        || phaseName.includes('MODEL');
    const routing = phaseName.includes('ROUTE') || phaseName.includes('DROP');

    container.classList.toggle('is-capturing', capturing);
    container.classList.toggle('is-analyzing', analysing);
    container.classList.toggle('is-routing', routing);
    container.classList.toggle(
        'is-belt-moving',
        !!(status && phaseName.includes('CONVEYOR')),
    );
}

function updateDistributorMotion(status) {
    const cards = [
        {
            card: document.getElementById('dist1-card'),
            position: Number(status.dist1_position) || 0,
            max: Math.max(1, Number(status.dist1_max) || 340),
            axis: String(status.dist1_state || 'IDLE').toUpperCase(),
            variable: '--dist1-turn',
        },
        {
            card: document.getElementById('dist2-card'),
            position: Number(status.dist2_position) || 0,
            max: Math.max(1, Number(status.dist2_max) || 340),
            axis: String(status.dist2_state || 'IDLE').toUpperCase(),
            variable: '--dist2-turn',
        },
    ];

    for (const item of cards) {
        const ratio = beltClamp(item.position / item.max, 0, 1);
        setBeltVar(item.variable, (ratio * 74).toFixed(2));
        if (!item.card) continue;
        const moving = ['MOVING', 'OPENING', 'CLOSING', 'HOMING']
            .includes(item.axis);
        item.card.classList.toggle('is-moving', moving);
        item.card.classList.toggle('is-open', item.axis === 'OPEN');
    }
}

// ─── Кадровый цикл: единственный источник движения ───────────

function advanceBelt(now) {
    const previous = belt.lastFrameAt || now;
    const dt = beltClamp((now - previous) / 1000, 0, BELT_MAX_FRAME_DT_S);
    belt.lastFrameAt = now;

    if (belt.holdActive) {
        belt.target += belt.direction * BELT_JOG_RATE_CELLS * dt;
    }

    const delta = belt.target - belt.phase;

    // Большой разрыв — это не ход ленты, а перевод счётчика: первый
    // ответ backend после запуска, пропущенные опросы или возврат
    // связи. Такое расхождение подхватывается мгновенно, иначе
    // интерфейс проехал бы движение, которого на линии не было.
    if (Math.abs(delta) > BELT_RESYNC_CELLS) {
        belt.phase = belt.target;
        belt.velocity = 0;
        belt.running = false;
        belt.intra = belt.source === 'CYCLE'
            ? beltClamp(belt.phase - belt.step, -1, 1)
            : 0;
        writeBeltVars();
        return false;
    }

    // Backend опрашивается реже, чем рисуются кадры, поэтому каждый
    // ответ приносит скачок координаты. Экспоненциальное сближение
    // растягивает этот скачок на интервал до следующего опроса: лента
    // идёт ровно, но никогда не обгоняет реальную позицию и не
    // продолжает ход после остановки.
    const follow = 1 - Math.exp(-dt / BELT_CATCHUP_TAU_S);
    const limit = BELT_CATCHUP_CELLS_PER_S * dt;
    let move = delta * follow;
    if (Math.abs(move) > limit) move = Math.sign(move) * limit;
    if (Math.abs(delta) < BELT_STOP_EPS) move = delta;

    belt.phase += move;
    belt.velocity = dt > 0 ? move / dt : 0;
    belt.running = Math.abs(belt.velocity) > BELT_STOP_EPS;
    if (!belt.running) belt.velocity = 0;

    belt.intra = belt.source === 'CYCLE'
        ? beltClamp(belt.phase - belt.step, -1, 1)
        : 0;

    writeBeltVars();
    return belt.running;
}

function writeBeltVars() {
    const speed = beltClamp(Math.abs(belt.velocity) / 1.2, 0, 1);

    setBeltVar('--belt-phase', belt.phase.toFixed(4));
    setBeltVar('--belt-intra', belt.intra.toFixed(4));
    setBeltVar('--belt-speed', speed.toFixed(3));
    setBeltVar('--belt-dir', String(belt.direction));
    setBeltVar('--belt-cadence', belt.cadence.toFixed(2));
    setBeltVar(
        '--belt-teeth',
        `${beltMod(belt.phase * (belt.cellPx || 96), BELT_TOOTH_PITCH_PX).toFixed(2)}px`,
    );
    setBeltVar('--drive-turn', beltMod(belt.phase * 360, 360).toFixed(2));
    setBeltVar('--drive-turn-fast', beltMod(belt.phase * 612, 360).toFixed(2));

    if (document.body) {
        const classes = document.body.classList;
        classes.toggle('belt-running', belt.running);
        classes.toggle('belt-forward', belt.direction >= 0);
        classes.toggle('belt-back', belt.direction < 0);
        for (const source of ['cycle', 'jog', 'nudge', 'idle']) {
            classes.toggle(
                `belt-src-${source}`,
                belt.source.toLowerCase() === source,
            );
        }
    }
    updateBeltReadout();
}

function updateBeltReadout() {
    const readout = document.getElementById('belt-readout');
    if (!readout) return;

    let text = 'ПРИВОД · СТОП';
    if (belt.source === 'CYCLE') {
        text = `ПРИВОД · ХОД ${Math.round(belt.progress * 100)}%`;
    } else if (BELT_READOUT_LABELS[belt.source]) {
        text = BELT_READOUT_LABELS[belt.source];
    }
    if (text === belt.lastReadout) return;
    belt.lastReadout = text;
    readout.textContent = text;
}

function beltFrame(timestamp) {
    belt.frameHandle = null;
    advanceBelt(typeof timestamp === 'number' ? timestamp : beltNow());
    refreshBeltLanes();
    measureBeltCell();
    scheduleBeltFrame();
}

function scheduleBeltFrame() {
    if (belt.frameHandle !== null) return;
    if (typeof requestAnimationFrame !== 'function') return;
    belt.frameHandle = requestAnimationFrame(beltFrame);
}

function startBeltDrive() {
    if (belt.started) return;
    belt.started = true;
    belt.lastFrameAt = beltNow();
    refreshBeltLanes(true);
    measureBeltCell();
    writeBeltVars();
    scheduleBeltFrame();
    if (typeof window !== 'undefined' && window.addEventListener) {
        window.addEventListener('resize', () => {
            belt.cellPx = 0;
            refreshBeltLanes(true);
            measureBeltCell();
        });
    }
}

// ─── Пусковой экран: та же лента показывает инициализацию ────

function setBootBeltProgress(percent) {
    const value = beltClamp(Number(percent) || 0, 0, 100);
    setBeltVar('--boot-progress', value.toFixed(2));
    setBeltVar(
        '--boot-teeth',
        `${beltMod(value * 1.8, BELT_TOOTH_PITCH_PX).toFixed(2)}px`,
    );
    refreshBeltLanes(true);
}
