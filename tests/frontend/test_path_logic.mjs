// test_path_logic.mjs — «Путь корпусов» соответствует фактической логике
// сортировки: вход 0, контроль +4, сортировка +7 с подготовкой маршрута
// и передачей между +7 и +8.
//
// Проверяет:
// - корпус на +7 не удерживается; лоток +8 и ворота показывают заранее
//   подготовленный канал маршрута;
// - сброс (dropping): во время фазы CONVEYOR маркер скользит из +7 в лоток
//   +8, а при исчезновении детали гаснет на месте, не «выкатываясь» дальше;
// - GOOD также передаётся следующим шагом через DIST1=0.
import { createSandbox, loadUI, installStubs, makeEl, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

// Стабильные ячейки: querySelectorAll возвращает одни и те же объекты,
// чтобы классы ячеек можно было проверять после отрисовки.
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

// Ворота: возвращаем стабильные заглушки, чтобы проверять их классы и текст.
const gates = { in: null, out: null };
sandbox.document.querySelector = (selector) => {
    if (selector === '.line-gate-in' || selector === '.line-gate-out') {
        const gate = makeEl('div');
        gate.className = selector;
        gate.textContent = selector === '.line-gate-in' ? '▸ ВХОД' : 'ВЫХОД ▸';
        if (selector === '.line-gate-in') gates.in = gate; else gates.out = gate;
        return gate;
    }
    return null;
};
sandbox._gates = gates;

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const lineCells = __els['line-cells'];
    const belt = _belt;
    const gates = _gates;
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
        phase, label: phase, positions: [0, 4], conveyor: { speed: 20000 },
        ...extra,
    });

    // ── 1. BAD на +7 ожидает подготовленный маршрут, без удержания ──
    updateLineCells([part(9, 7, 'BAD')], proc('ROUTE_WAIT', { positions: [7] }));

    const cell7 = findCell(7);
    const cell8 = findCell(8);
    assert(!cell7.classList._set.has('cell-hold'), '+7 is not a hold cell');
    assert(cell8.classList._set.has('line-cell-chute'), 'chute cell present at +8');
    assert(cell8.classList._set.has('chute-bad'), 'chute shows BAD channel while held');

    const token9 = findToken(9);
    assert(token9, 'token #9 exists');
    assert(!token9.classList._set.has('token-hold'), 'token is not held');
    assert(token9.style.left === '350px', 'waiting token stays at +7');
    assert(token9.style.opacity === '1', 'held token visible');

    assert(gates.out.textContent === '▼ БРАК', 'exit gate shows BAD channel: ' + gates.out.textContent);
    assert(gates.out.classList._set.has('gate-rejecting'), 'exit gate rejecting class');

    // Route preparation sets gates before the belt starts. The token remains
    // at +7 and is not marked as mechanically held.
    updateLineCells([part(9, 7, 'BAD', { dropping: true })], proc('ROUTE_PREPARE', { positions: [7] }));
    const token9prep = findToken(9);
    assert(token9prep.style.left === '350px', 'route preparation keeps token at +7');
    assert(!token9prep.classList._set.has('token-hold'), 'route preparation has no hold marker');
    assert(!token9prep.classList._set.has('token-dropping'), 'route preparation is not yet a visual drop');
    assert(token9prep.style.opacity === '1', 'route preparation keeps token visible');

    // ── 2. Сброс: лента несёт корпус между +7 и +8 ──
    updateLineCells([part(9, 7, 'BAD', { dropping: true })], proc('CONVEYOR_MOVING', { positions: [0,1,2,3,4,5,6,7] }));
    const token9b = findToken(9);
    assert(token9b, 'token #9 still present');
    assert(token9b.classList._set.has('token-dropping'), 'dropping token marked');
    assert(token9b.classList._set.has('token-in-chute'), 'dropping token is marked in chute');
    assert(token9b.style.opacity === '1', 'dropping token is fully opaque in chute');
    assert(token9b.style.left === '400px', 'token slid into chute +8: ' + token9b.style.left);
    assert(cell8.classList._set.has('chute-bad'), 'chute stays BAD while dropping');
    assert(cell8.classList._set.has('chute-occupied'), 'chute hides its base symbol while occupied');

    // ── 3. Деталь ушла: маркер гаснет в лотке, а не выкатывается за линию ──
    updateLineCells([], proc('SETTLE'));
    const token9c = findToken(9);
    assert(token9c, 'token #9 still in DOM until fade completes');
    assert(token9c.style.opacity === '0', 'dropped token fades in place');
    assert(token9c.style.left === '400px', 'dropped token does not roll past the chute');
    assert(cell8.classList._set.has('chute-occupied'), 'chute remains covered during fade');

    // Если следующий сброс пришёл до окончания fade-out, старый маркер
    // удаляется перед входом нового: в +8 не складываются два корпуса.
    updateLineCells([part(11, 7, 'BAD', { dropping: true })], proc('CONVEYOR_MOVING'));
    const token11 = findToken(11);
    assert(token11 && token11.classList._set.has('token-in-chute'),
        'next dropping token enters chute');
    assert(!findToken(9), 'previous chute token removed before next entry');
    const chuteTokens = lineCells.children.filter(child => child.dataset.partId
        && child.style.left === '400px');
    assert(chuteTokens.length === 1, 'only one token occupies chute: ' + chuteTokens.length);

    // ── 4. GOOD на +7: передачи ещё не было, ворота показывают GOOD ──
    updateLineCells([part(4, 7, 'GOOD')], proc('ROUTE_CHECK', { positions: [7] }));
    const cell7b = findCell(7);
    const cell8b = findCell(8);
    assert(!cell7b.classList._set.has('cell-hold'), 'GOOD part is not held at +7');
    assert(!cell8b.classList._set.has('chute-bad') && !cell8b.classList._set.has('chute-cleanup'),
        'chute neutral for GOOD pass');
    const token4 = findToken(4);
    assert(token4 && !token4.classList._set.has('token-hold'), 'GOOD token not marked as held');
    assert(gates.out.textContent === 'ПРОХОД ▸', 'exit gate shows PASS: ' + gates.out.textContent);

    // ── 5. CLEANUP на +7 подсвечивает лоток жёлтым каналом ──
    updateLineCells([part(6, 7, 'CLEANUP')], proc('ROUTE_WAIT', { positions: [7] }));
    const cell8c = findCell(8);
    assert(cell8c.classList._set.has('chute-cleanup'), 'chute shows CLEANUP channel');
    assert(gates.out.textContent === '▼ ОЧИСТКА', 'exit gate shows CLEANUP: ' + gates.out.textContent);

    // ── 6. Без корпуса на сортировке ворота выхода нейтральны ──
    gates.out.textContent = '';
    gates.out.className = '';
    updateLineCells([part(5, 3, 'GOOD')], proc('ANALYSIS_REVIEW'));
    assert(gates.out.textContent === 'ВЫХОД ▸', 'neutral exit gate: ' + gates.out.textContent);

    // ── 7. Подтверждение движения не делает второй визуальный шаг ──
    // CONVEYOR_CONFIRMED приходит уже после того, как backend увеличил
    // position. Раньше проверка includes('CONVEYOR') сдвигала маркер
    // ещё на одну ячейку, а на SETTLE он отскакивал обратно.
    updateLineCells([part(10, 4, 'BAD')], proc('CONVEYOR_MOVING'));
    const token10 = findToken(10);
    assert(token10 && token10.style.left === '250px',
        'token #10 follows the moving belt to +5');
    assert(belt.classList._set.has('moving'), 'belt is animated during transport');
    updateLineCells([part(10, 5, 'BAD')], proc('CONVEYOR_CONFIRMED'));
    assert(!belt.classList._set.has('moving'), 'belt stops after confirmation');
    assert(token10.style.left === '250px',
        'confirmed position does not advance token a second time: ' + token10.style.left);
    assert(__els['process-phase-label'].textContent === 'РАБОТАЕТ',
        'internal confirmed phase does not replace the line state');
    updateLineCells([part(10, 5, 'BAD')], proc('SETTLE'));
    assert(token10.style.left === '250px',
        'token does not jump back after confirmed phase: ' + token10.style.left);

    console.log('TEST PATH LOGIC OK');
`;

await runInSandbox(sandbox, body);
