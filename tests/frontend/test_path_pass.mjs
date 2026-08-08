// test_path_pass.mjs — «Путь корпусов»: проход годного синхронен с лентой.
//
// Backend снимает годный корпус с учёта на ROUTE_CHECK (статичная фаза,
// лента стоит), а физически корпус падает с +7 на следующем шаге движения:
// годный при DIST1 на концевике падает в статику — в той же зоне +8, что
// и реджект, только без цвета канала. Поэтому маркер:
//  - на статичных фазах паркуется на +7 (не скользит и не гаснет);
//  - с ближайшей транспортной фазой падает в +8 и гаснет там («нырок»);
//  - если движения долго нет (линия остановилась) — гаснет на месте по
//    запасному таймеру.
// Блок «СОСТОЯНИЕ ЛЕНТЫ» показывает состояние автомата, а не внутреннюю
// фазу ROUTE_PREPARE/ROUTE_CHECK. Held-детали серии рисуются так же, как у
// первой детали маршрута.
import { createSandbox, loadUI, installStubs, makeEl, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

const cellsCache = Array.from({ length: 9 }, (_, index) => {
    const cell = makeEl('div');
    cell.dataset.pos = String(index);
    cell.getBoundingClientRect = () => ({
        left: index * 50, top: 0, width: 44, height: 36,
    });
    return cell;
});
const lineCells = sandbox.__els['line-cells'];
const belt = makeEl('div');
lineCells.querySelector = (selector) =>
    selector === '.conveyor-belt' ? belt : null;
lineCells.querySelectorAll = (selector) =>
    selector === '.line-cell[data-pos]' ? cellsCache : [];
sandbox._belt = belt;

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const lineCells = __els['line-cells'];
    const findCell = (pos) => lineCells.querySelectorAll('.line-cell[data-pos]')
        .find(c => c.dataset.pos === String(pos));
    const findToken = (id) => lineCells.children.find(c => c.dataset.partId === String(id));

    state.splashActive = false;
    state.bootDone = true;
    state.lineState = 'RUNNING';
    updateProcessPhaseLabel(state.lineState);
    state.pendingAnalysisVersion = null;
    state.pendingFlushTimer = null;

    const part = (id, position, category, extra = {}) => ({
        id, position, category, held: false, dropping: false, ...extra,
    });
    const proc = (phase, extra = {}) => ({
        phase, label: phase, positions: [0, 4], conveyor: { speed: 20000 }, ...extra,
    });
    const beltPos = [0, 1, 2, 3, 4, 5, 6, 7];

    // ── 1. Годный доехал до +7 ──
    updateLineCells([part(4, 7, 'GOOD')], proc('CONVEYOR_CONFIRMED', { positions: beltPos }));
    assert(findToken(4).style.left === '350px', 'GOOD token stands at +7');

    // ── 2. ROUTE_CHECK снял корпус с учёта: маркер паркуется на +7 ──
    updateLineCells([], proc('ROUTE_CHECK', { positions: [7] }));
    const t4b = findToken(4);
    assert(t4b, 'pass token parked, still in DOM');
    assert(t4b.style.left === '350px',
        'pass token must not slide on a static phase: ' + t4b.style.left);
    assert(t4b.style.opacity === '1', 'pass token stays visible while belt stands');

    updateLineCells([], proc('STEP_COMPLETE'));
    const t4c = findToken(4);
    assert(t4c && t4c.style.left === '350px' && t4c.style.opacity === '1',
        'pass token stays parked until the belt moves');

    // ── 3. Следующий шаг ленты: падение в статику вместе с движением ──
    updateLineCells([], proc('CONVEYOR_MOVING', { positions: beltPos }));
    const t4d = findToken(4);
    assert(t4d, 'pass token still present while animating the pass');
    assert(t4d.style.left === '400px',
        'pass token falls into the +8 statics with the belt: ' + t4d.style.left);
    assert(t4d.style.opacity === '0', 'pass token fades while falling');
    assert(!t4d.classList._set.has('token-dropping'),
        'passing good token is not marked as a drop');
    assert(findCell(8).classList._set.has('chute-occupied'),
        'chute symbol hidden while the pass fade plays');
    assert(!findCell(8).classList._set.has('chute-bad')
        && !findCell(8).classList._set.has('chute-cleanup'),
        'chute stays neutral for the statics channel');
    assert(_belt.classList._set.has('moving'), 'belt animated during the physical pass');

    __flushTimers();   // парк-таймер уже неактивен; удаление после проезда
    assert(!findToken(4), 'pass token removed after the animated pass');

    // ── 4. Внутренняя фаза маршрута не подменяет состояние линии ──
    updateLineCells([], proc('ROUTE_PREPARE'));
    assert(__els['process-phase-label'].textContent === 'РАБОТАЕТ',
        'ROUTE_PREPARE keeps line state: ' + __els['process-phase-label'].textContent);
    updateLineCells([], proc('ROUTE_CHECK'));
    assert(__els['process-phase-label'].textContent === 'РАБОТАЕТ',
        'ROUTE_CHECK keeps line state: ' + __els['process-phase-label'].textContent);

    // ── 5. Деталь серии на +7 рисуется так же, как первая деталь маршрута ──
    updateLineCells([part(8, 7, 'CLEANUP', { held: true })], proc('ANALYSIS_REVIEW'));
    assert(findCell(7).classList._set.has('cell-hold'), 'series part at +7 gets the hold cell');
    assert(findToken(8).classList._set.has('token-hold'), 'series token marked held');
    assert(findCell(8).classList._set.has('chute-cleanup'), 'chute keeps the CLEANUP channel');

    // ── 6. Парк без движения: запасной таймер гасит маркер на месте ──
    updateLineCells([], proc('SETTLE'));
    __flushTimers();
    updateLineCells([part(7, 7, 'GOOD')], proc('CONVEYOR_CONFIRMED', { positions: beltPos }));
    updateLineCells([], proc('ROUTE_CHECK', { positions: [7] }));
    __flushTimers();   // движения нет — сработал запасной таймер парковки
    const t7 = findToken(7);
    assert(t7 && t7.style.opacity === '0' && t7.style.left === '350px',
        'parked pass fades in place when no belt phase comes');
    __flushTimers();   // удаление после затухания
    assert(!findToken(7), 'parked pass removed after the fallback fade');

    console.log('TEST PATH PASS OK');
`;

await runInSandbox(sandbox, body);
