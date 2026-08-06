// test_single_run_ui.mjs — рендер компактной панели анализа при одиночном прогоне.
//
// Тройное голосование убрано: у каждого порога ровно один замер, бейджи
// правил не показывают счётчики «· 2/3», у замеров нет data-run (переключение
// кадров недоступно). Тест работает с реальным DOM из index.html: только
// компактная панель (fa-new-*), «полная» панель (frame-analysis-cards и т.п.)
// в production отсутствует.
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const tbody = __els['fa-new-tbody'];
    const verdict = __els['fa-new-verdict'];
    const context = __els['fa-new-context'];
    const panel = __els['frame-analysis-panel'];

    // ── Регрессия: els.frameAnalysisPanel — валидный алиас панели ──
    assert(els.frameAnalysisPanel === els.frameAnalysisPanelNew,
        'frameAnalysisPanel alias resolves to the real panel');

    // ── Бейджи: один прогон — без счётчика голосов ──
    assert(faNewVoteSummary({ decision: 'present', present_votes: 1, total_runs: 1, required_votes: 1 }).text === 'КОРПУС',
        'present badge without count');
    assert(faNewVoteSummary({ decision: 'triggered', triggered_votes: 1, total_runs: 1, required_votes: 1 }).text === 'СРАБОТАЛО',
        'triggered badge without count');
    assert(faNewVoteSummary({ decision: 'empty', empty_votes: 1, total_runs: 1, required_votes: 1 }).text === 'ПУСТО',
        'empty badge without count');
    // Мультипрогоновые данные (если INSPECTION_RUNS вернётся > 1) всё ещё показывают счётчик
    assert(faNewVoteSummary({ decision: 'normal', normal_votes: 2, total_runs: 3, required_votes: 2 }).text.includes('/3'),
        'multi-run badge keeps count');

    // ── Компактная панель: один замер на порог, без data-run ──
    const report = {
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА',
        message: 'итог по свежему кадру',
        picture_run: 1, picture_reason: 'window_geometry: замер ближе всего к порогу',
        rules: [{
            name: 'part_presence', triggered: false, part_absent: false,
            vote_details: { decision: 'present', present_votes: 1, total_runs: 1, required_votes: 1 },
            run_cards: [[{ role: 'INPUT', ok: true, metrics: [
                { label: 'Flatness L', value: '12', limit: '30', ok: true,
                  value_raw: 12, limit_raw: 30, key: 'flatness_left' },
            ] }]],
        }],
        models: [],
    };
    renderNewFrameAnalysis(report);

    assert(!panel.classList._set.has('is-collapsed'), 'panel expanded');
    assert(verdict.textContent === 'ГОДНО', 'verdict: ' + verdict.textContent);
    assert(context.textContent.includes('ВХОД'), 'context has stage');
    assert(context.textContent.includes('КОРПУС #7'), 'context has part id');

    let measCount = 0;
    (function collect(node) {
        node.children.forEach(c => {
            if (c.classList && c.classList._set.has('fa-new-meas')) measCount++;
            collect(c);
        });
    })(tbody);
    assert(measCount === 1, 'exactly one measurement chip: ' + measCount);

    // Сработавшее правило → вердикт «БРАК» с названием правила
    const badReport = {
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА', message: 'x', picture_run: 1,
        rules: [{
            name: 'window_geometry', triggered: true, part_absent: false,
            vote_details: { decision: 'triggered', triggered_votes: 1, total_runs: 1, required_votes: 1 },
            run_cards: [[{ role: 'INPUT_LEFT', ok: false, metrics: [
                { label: 'Смещение, px', value: '18.0', limit: '15.0', ok: false,
                  value_raw: 18.0, limit_raw: 15.0, key: 'shift' },
            ] }]],
        }],
        models: [],
    };
    renderNewFrameAnalysis(badReport);
    assert(verdict.textContent.includes('БРАК'), 'bad verdict: ' + verdict.textContent);

    // ── Замеры (faNewCollectThresholds): один слот на порог ──
    const runCards = [[{
        role: 'INPUT_LEFT', ok: true, verdict: 'в допуске', found: [],
        metrics: [{ label: 'Смещение, px', value: '12.0', limit: '15.0', ok: true,
                    value_raw: 12.0, limit_raw: 15.0, key: 'shift' }],
    }]];
    const thrMap = faNewCollectThresholds(runCards);
    const thr = thrMap.get('shift');
    assert(thr, 'threshold collected');
    assert(thr.runs.length === 1, 'one run slot');

    // ── Мёртвая «полная» панель: функции не падают, но DOM отсутствует ──
    // renderFrameAnalysisPanel в production упирается в frame-analysis-cards
    // (нет в HTML) и должен безопасно выйти.
    assert(__els['frame-analysis-cards'] === null || typeof __els['frame-analysis-cards'] === 'undefined',
        'full panel cards DOM absent in production');
    let threw = false;
    try {
        renderFrameAnalysisPanel([{
            name: 'window_geometry', triggered: true, skipped: false,
            part_absent: false, run_cards: badReport.rules[0].run_cards,
            threshold_breaches: [], threshold_conclusion: 'x',
        }], 1, [], { totalRules: 1 });
    } catch (error) {
        threw = true;
    }
    assert(!threw, 'renderFrameAnalysisPanel no-crash with absent DOM');

    // ── Структуризация: один объект — один блок со всеми его замерами ──
    const text = (el) => el ? (el.textContent || '') : '';
    const objReport = {
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА', message: 'x', picture_run: 1,
        rules: [{
            name: 'window_geometry', triggered: false, part_absent: false,
            vote_details: { decision: 'normal', normal_votes: 1, total_runs: 1, required_votes: 1 },
            run_cards: [[{ role: 'INPUT_LEFT', ok: true, metrics: [
                { label: 'Найдено окон, шт', value: '7', limit: '7', ok: true,
                  value_raw: 7, limit_raw: 7, key: 'found' },
                { label: 'Окно #1: верх, px', value: '25', limit: '20…40 px', ok: true,
                  value_raw: 25, key: 'window_1_top_px', object: 'Окно #1' },
                { label: 'Окно #1: низ, px', value: '30', limit: '20…40 px', ok: true,
                  value_raw: 30, key: 'window_1_bottom_px', object: 'Окно #1' },
                { label: 'Окно #2: верх, px', value: '45', limit: '20…40 px', ok: false,
                  value_raw: 45, key: 'window_2_top_px', object: 'Окно #2' },
                { label: 'Окно #2: низ, px', value: '30', limit: '20…40 px', ok: true,
                  value_raw: 30, key: 'window_2_bottom_px', object: 'Окно #2' },
            ]}]],
        }],
        models: [],
    };
    const objLs = {
        total: 12, good: 8, rejected: 3, cleanup: 1,
        line_parts: [{ id: 7, position: 0, category: 'BAD' }],
    };
    renderNewFrameAnalysis(objReport, objLs);
    const blocks = tbody.children.filter(c => c.className.includes('fa-new-obj-block'));
    assert(blocks.length === 2, 'two object blocks: ' + blocks.length);
    assert(text(blocks[0].children[0].children[0]) === 'Окно #1', 'block 1 name');
    assert(text(blocks[1].children[0].children[0]) === 'Окно #2', 'block 2 name');
    assert(blocks[0].children[0].children[1].classList._set.has('ok'), 'block 1 badge ok');
    assert(blocks[1].children[0].children[1].classList._set.has('bad'), 'block 2 badge bad');
    // В блоке окна все его замеры, подпись без префикса «Окно #N:»
    const win1Rows = blocks[0].children.slice(1);
    assert(win1Rows.length === 2, 'window #1 rows: ' + win1Rows.length);
    assert(text(win1Rows[0].children[0]) === 'верх, px', 'stripped label: ' + text(win1Rows[0].children[0]));
    // Общие замеры правила остаются вне блоков
    const generalRows = tbody.children.filter(c => c.className === 'fa-new-thr-row');
    assert(generalRows.length === 1, 'general rows: ' + generalRows.length);
    assert(text(generalRows[0].children[0]) === 'Найдено окон, шт', 'general label');
    // Вердикт по категории корпуса на линии (БРАК)
    assert(text(verdict).includes('БРАК'), 'verdict from line category: ' + text(verdict));

    // ── Статистика корпусов (всего / годные / брак / очистка) ──
    assert(text(__els['fa-new-stat-total']) === '12', 'stat total');
    assert(text(__els['fa-new-stat-good']) === '8', 'stat good');
    assert(text(__els['fa-new-stat-bad']) === '3', 'stat bad');
    assert(text(__els['fa-new-stat-cleanup']) === '1', 'stat cleanup');

    // ── Анти-мигание: повторный рендер тех же данных не перестраивает DOM ──
    const sameGeneral = generalRows[0];
    renderNewFrameAnalysis(objReport, objLs);
    assert(tbody.children.filter(c => c.className === 'fa-new-thr-row')[0] === sameGeneral,
        'DOM not rebuilt on identical report');
    assert(tbody.children.filter(c => c.className === 'fa-new-thr-row').length === 1,
        'no duplicated rows');

    // ── Очистка: категория CLEANUP → вердикт ОЧИСТКА ──
    renderNewFrameAnalysis({ ...objReport, rules: [{
        name: 'glass', triggered: true, part_absent: false,
        vote_details: { decision: 'triggered', triggered_votes: 1, total_runs: 1, required_votes: 1 },
        run_cards: [[{ role: 'TOP', ok: false, metrics: [
            { label: 'Совпадений стекла, шт', value: '1', limit: '0', ok: false,
              value_raw: 1, limit_raw: 0, key: 'glass_hits' },
            { label: 'Стекло #1: платформа, px', value: '5', limit: '0', ok: false,
              value_raw: 5, limit_raw: 0, key: 'glass_1_platform_px', object: 'Стекло #1' },
        ]}]],
    }] }, { ...objLs, line_parts: [{ id: 7, position: 4, category: 'CLEANUP' }] });
    assert(text(verdict) === 'ОЧИСТКА', 'verdict CLEANUP: ' + text(verdict));
    const glassBlock = tbody.children.filter(c => c.className.includes('fa-new-obj-block'))[0];
    assert(glassBlock, 'glass block present');
    assert(text(glassBlock.children[0].children[0]) === 'Стекло #1', 'glass block name');
    assert(text(glassBlock.children[1].children[0]) === 'платформа, px', 'glass stripped label');

    // ── Пустые правила между шагами: плейсхолдер, панель не схлопнута ──
    renderNewFrameAnalysis({
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        title: 'x', message: 'x', rules: [], models: [],
    }, objLs);
    assert(!panel.classList._set.has('is-collapsed'), 'panel stays open on empty rules');
    assert(tbody.children.length === 1 && tbody.children[0].className === 'fa-new-empty',
        'placeholder row shown');

    // ── Группировка по объектам делит одинаковые номера разных камер ──
    const grouped = faNewCollectGroups([[
        { role: 'SPIDER_LEFT', ok: true, metrics: [
            { label: 'Контакт #1: откл. верх, px', value: '2', limit: '5', ok: true,
              key: 'contact_1_dev_top_px', object: 'Контакт #1' },
        ]},
        { role: 'SPIDER_RIGHT', ok: true, metrics: [
            { label: 'Контакт #1: откл. верх, px', value: '9', limit: '5', ok: false,
              key: 'contact_1_dev_top_px', object: 'Контакт #1' },
        ]},
    ]]);
    assert(grouped.objects.length === 2, 'objects split by camera role');
    assert(grouped.objects[0].rows[0].value === '2' && grouped.objects[1].rows[0].value === '9',
        'per-role values kept');

    console.log('TEST SINGLE RUN UI OK');
`;

await runInSandbox(sandbox, body);
