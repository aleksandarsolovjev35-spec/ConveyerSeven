// frame-analysis.js — компактный анализ кадра.
//
// Структура панели: правило → общие замеры правила → блоки объектов.
// Правило «один объект — один блок со всеми замерами, касающимися его»:
// метрики с одинаковым полем object (Окно #N, Контакт #N, Раковина #N,
// Стекло #N …) собираются в один блок; метрики без object (агрегаты и
// пороги правила целиком) остаются в общем списке под заголовком правила.
//
// Анти-мигание: при одинаковом содержимом отчёта DOM не перестраивается
// (только вердикт, контекст и статистика корпусов обновляются точечно),
// панель не схлопывается при временно пустых данных между шагами, а
// позиция прокрутки сохраняется при перерисовке того же корпуса.
'use strict';

const FA_RULE_NAMES = {
    part_presence: 'Наличие корпуса',
    window_geometry: 'Геометрия входа',
    window_sinks: 'Раковины в окнах',
    contacts_long: 'Длинные контакты',
    contacts_short: 'Короткие контакты',
    long_omission: 'Длинная полоса',
    short_omission: 'Короткая полоса',
    top_contacts: 'Контакты сверху',
    top_platform: 'Платформа',
    platform_contacts_overlap: 'Заплыв платформы',
    sinks: 'Раковины корпуса',
    glass: 'Стекло',
    glass_on_contacts: 'Стекло на контактах',
};

// Ключ последнего отрисованного отчёта и контекста прокрутки.
let _faLastKey = null;
let _faLastScrollContext = null;
let _faLastStatsKey = null;

function faNewVoteSummary(vote) {
    if (!vote) return {className: 'ok', text: '—'};
    const total = Number(vote.total_runs) || 3;
    // Тройное голосование убрано: при одном прогоне счётчик не показываем.
    const single = total <= 1;
    const count = (value) => single ? '' : ' · ' + (value ?? 0) + '/' + total;
    if (vote.decision === 'empty') {
        return {className: 'warn', text: 'ПУСТО' + count(vote.empty_votes ?? vote.triggered_votes)};
    }
    if (vote.decision === 'present') {
        return {className: 'ok', text: 'КОРПУС' + count(vote.present_votes ?? vote.normal_votes)};
    }
    if (vote.decision === 'triggered') {
        return {className: 'bad', text: 'СРАБОТАЛО' + count(vote.triggered_votes)};
    }
    return {className: 'ok', text: 'НОРМА' + count(vote.normal_votes)};
}

function faNewFormatValue(v) {
    if (v == null || v === '') return '—';
    return String(v);
}

function faNewFormatLimit(metric) {
    if (!metric) return '—';
    if (metric.limit != null && metric.limit !== '') return String(metric.limit);
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) return String(metric.limit_raw);
    return '—';
}

function faNewCollectThresholds(runCards) {
    // Собираем все уникальные пороги по всем прогонам.
    // key: metric_key (или label если нет key), value: {label, limit, runs: [val_or_null, ...]}
    const map = new Map();
    const runs = Array.isArray(runCards) ? runCards : [];
    runs.forEach((cards, runIndex) => {
        const list = Array.isArray(cards) ? cards : [];
        for (const card of list) {
            const metrics = Array.isArray(card.metrics) ? card.metrics : [];
            for (const m of metrics) {
                const key = m.key || m.label;
                if (!key) continue;
                if (!map.has(key)) {
                    map.set(key, {
                        label: m.label || m.key || '—',
                        key: m.key || null,
                        limit: m.limit || null,
                        limit_raw: m.limit_raw,
                        runs: runs.map(() => null),
                    });
                }
                const entry = map.get(key);
                if (m.limit != null && m.limit !== '') entry.limit = m.limit;
                if (m.limit_raw !== undefined) entry.limit_raw = m.limit_raw;
                if (m.label) entry.label = m.label;
                entry.runs[runIndex] = {
                    value: m.value != null ? m.value : null,
                    ok: m.ok == null ? null : !!m.ok,
                    value_raw: typeof m.value_raw === 'number' ? m.value_raw : null,
                };
            }
        }
    });
    return map;
}

// ─── Группировка замеров: общие + блоки объектов ─────────────

function faNewCollectGroups(runCards) {
    // Возвращает {general: [row, ...], objects: [{name, rows: [row, ...]}, ...]}.
    // Строка: {label, limit, value, ok}.
    const generalMap = new Map();
    const objectsMap = new Map();
    const runs = Array.isArray(runCards) ? runCards : [];
    runs.forEach((cards) => {
        const list = Array.isArray(cards) ? cards : [];
        for (const card of list) {
            const role = card.role || '';
            const metrics = Array.isArray(card.metrics) ? card.metrics : [];
            for (const m of metrics) {
                const key = m.key || m.label;
                if (!key) continue;
                const row = {
                    label: m.label || m.key || '—',
                    limit: faNewFormatLimit(m),
                    value: faNewFormatValue(m.value != null ? m.value : null),
                    ok: m.ok == null ? null : !!m.ok,
                    value_raw: typeof m.value_raw === 'number' ? m.value_raw : null,
                };
                const objectName = m.object || null;
                if (!objectName) {
                    if (generalMap.has(key)) continue;
                    generalMap.set(key, row);
                } else {
                    // Роль в ключе группы: у разных камер одинаковые номера
                    // объектов не смешиваются.
                    const groupKey = role + '::' + objectName;
                    let group = objectsMap.get(groupKey);
                    if (!group) {
                        group = {name: objectName, rowsMap: new Map()};
                        objectsMap.set(groupKey, group);
                    }
                    if (group.rowsMap.has(key)) continue;
                    group.rowsMap.set(key, row);
                }
            }
        }
    });
    return {
        general: [...generalMap.values()],
        objects: [...objectsMap.values()].map(g => ({
            name: g.name,
            rows: [...g.rowsMap.values()],
        })),
    };
}

function faNewObjectStatus(rows) {
    let hasBad = false;
    let hasOk = false;
    let measured = false;
    for (const row of rows) {
        if (row.ok == null) continue;
        measured = true;
        if (row.ok) hasOk = true;
        else hasBad = true;
    }
    if (hasBad) return {cls: 'bad', text: 'ОТКЛОНЕНИЕ'};
    if (measured && hasOk) return {cls: 'ok', text: 'В НОРМЕ'};
    return {cls: 'muted', text: '—'};
}

// Внутри блока объекта префикс «Окно #1: » в подписи не нужен —
// объект уже назван в заголовке блока.
function faNewStripObjectPrefix(label) {
    return String(label).replace(
        /^(?:Окно|Контакт|Раковина|Стекло|Shell|Glass)\s*#\d+(?:\s*\([^)]*\))?(?:\s*→\s*(?:контакт\s*)?#\d+)?\s*:\s*/i,
        '',
    );
}

// ─── Ключ отчёта (анти-мигание) ──────────────────────────────

function faNewReportKey(report) {
    try {
        return JSON.stringify({
            kind: report.kind,
            role: report.role,
            stage: report.stage,
            part_id: report.part_id,
            updated_at: report.updated_at,
            picture_run: report.picture_run,
            rules: report.rules,
        });
    } catch (error) {
        return [report.kind, report.role, report.stage, report.part_id, report.updated_at].join('|');
    }
}

// Вердикт: по отсутствию корпуса, по категории корпуса на линии
// (ГОДНОЕ / БРАК / ОЧИСТКА) и по сработавшим правилам.
function faNewVerdict(report, ls) {
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const absent = rules.find(r => r && r.part_absent === true);
    if (absent) return {cls: 'warn', text: 'КОРПУС НЕ ОБНАРУЖЕН'};

    const triggered = rules.filter(r => r && r.triggered === true);

    let category = null;
    if (ls && report.part_id != null) {
        const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
        const part = parts.find(p => Number(p.id) === Number(report.part_id));
        if (part) category = String(part.category || '').toUpperCase();
    }

    if (category === 'CLEANUP') return {cls: 'warn', text: 'ОЧИСТКА'};
    if (triggered.length) {
        const names = triggered.map(r => FA_RULE_NAMES[r.name] || r.name).join(', ');
        return {cls: 'bad', text: 'БРАК: ' + names};
    }
    if (category === 'BAD') return {cls: 'bad', text: 'БРАК'};
    if (rules.some(r => r && r.skipped === true)) {
        return {cls: 'warn', text: 'ЕСТЬ ПРОПУЩЕННЫЕ ПРАВИЛА'};
    }
    if (category === 'GOOD') return {cls: 'ok', text: 'ГОДНОЕ'};
    return {cls: 'ok', text: 'ГОДНО'};
}

// Статистика корпусов (всего / годные / брак / очистка): обновляется
// точечно, без пересборки DOM.
function faNewUpdateStats(ls) {
    const totalEl = document.getElementById('fa-new-stat-total');
    if (!totalEl) return;
    const total = ls ? (Number(ls.total) || 0) : 0;
    const good = ls ? (Number(ls.good) || 0) : 0;
    const bad = ls ? (Number(ls.rejected) || 0) : 0;
    const cleanup = ls ? (Number(ls.cleanup) || 0) : 0;
    const key = [total, good, bad, cleanup].join('|');
    if (key === _faLastStatsKey) return;
    _faLastStatsKey = key;
    setIfChanged(document.getElementById('fa-new-stat-total'), total);
    setIfChanged(document.getElementById('fa-new-stat-good'), good);
    setIfChanged(document.getElementById('fa-new-stat-bad'), bad);
    setIfChanged(document.getElementById('fa-new-stat-cleanup'), cleanup);
}

// ─── Построение строк и блоков ───────────────────────────────

function faNewBuildRow(row, isPic) {
    const rowEl = document.createElement('div');
    rowEl.className = 'fa-new-thr-row';

    const label = document.createElement('span');
    label.className = 'fa-new-thr-label';
    label.textContent = row.label;
    rowEl.appendChild(label);

    const limit = document.createElement('span');
    limit.className = 'fa-new-thr-limit';
    limit.textContent = row.limit;
    rowEl.appendChild(limit);

    const meas = document.createElement('span');
    meas.className = 'fa-new-meas' +
        (row.ok === false ? ' is-bad' : '') +
        (row.ok === true ? ' is-ok' : '') +
        (isPic ? ' is-pic' : '');
    meas.textContent = row.value;
    rowEl.appendChild(meas);
    return rowEl;
}

function renderNewFrameAnalysis(report, ls) {
    const panel = document.getElementById('frame-analysis-panel');
    const tbody = document.getElementById('fa-new-tbody');
    if (!panel || !tbody) return;

    // Статистика корпусов обновляется всегда, но без пересборки DOM.
    faNewUpdateStats(ls);

    const available = report.available === true;
    if (!available) {
        if (!panel.classList.contains('is-collapsed')) {
            panel.classList.add('is-collapsed');
        }
        tbody.replaceChildren();
        _faLastKey = null;
        _faLastScrollContext = null;
        return;
    }
    if (panel.classList.contains('is-collapsed')) {
        panel.classList.remove('is-collapsed');
    }

    const rules = Array.isArray(report.rules) ? report.rules : [];

    // Вердикт и контекст — точечные обновления.
    const verdictEl = document.getElementById('fa-new-verdict');
    if (verdictEl) {
        const verdict = faNewVerdict(report, ls);
        const verdictCls = 'fa-new-verdict ' + verdict.cls;
        if (verdictEl.className !== verdictCls) {
            verdictEl.className = verdictCls;
        }
        setIfChanged(verdictEl, verdict.text);
    }
    const contextEl = document.getElementById('fa-new-context');
    if (contextEl) {
        const parts = [];
        const stage = String(report.stage || '').toUpperCase();
        if (stage) parts.push(stage);
        if (report.role) {
            parts.push(typeof cameraRoleLabel === 'function'
                ? cameraRoleLabel(report.role)
                : report.role);
        }
        if (report.part_id != null) parts.push('КОРПУС #' + report.part_id);
        setIfChanged(contextEl, parts.join(' · '));
    }

    // Содержимое не изменилось — DOM не трогаем (анти-мигание).
    const key = faNewReportKey(report);
    if (key === _faLastKey && tbody.children.length) {
        return;
    }
    _faLastKey = key;
    tbody.replaceChildren();

    const scrollEl = document.getElementById('fa-new-body');
    const keepScroll = scrollEl ? scrollEl.scrollTop : 0;
    const scrollContext = String(report.stage || '') + '|' + (report.part_id == null ? '' : report.part_id);
    const resetScroll = _faLastScrollContext !== null && _faLastScrollContext !== scrollContext;

    // Данных ещё нет (между шагами или другая камера): панель остаётся
    // раскрытой с плейсхолдером, а не мигает схлопыванием/раскрытием.
    if (!rules.length) {
        const emptyRow = document.createElement('div');
        emptyRow.className = 'fa-new-empty';
        emptyRow.textContent = 'Ожидание результатов анализа…';
        tbody.appendChild(emptyRow);
        return;
    }

    const frag = (typeof document.createDocumentFragment === 'function')
        ? document.createDocumentFragment()
        : null;
    const put = frag ? (el) => frag.appendChild(el) : (el) => tbody.appendChild(el);

    // Сортировка: сработавшие → пропущенные → нормальные
    const sorted = [...rules].sort((a, b) => {
        const order = r => r.part_absent ? 0 : (r.triggered ? 1 : (r.skipped ? 2 : 3));
        return order(a) - order(b);
    });

    const pictureRun = Number(report.picture_run) || 0;
    const isPic = pictureRun === 1;

    for (const rule of sorted) {
        // Заголовок правила
        const ruleHead = document.createElement('div');
        ruleHead.className = 'fa-new-rule-head' + (rule.triggered || rule.part_absent ? ' triggered' : '');
        const ruleName = document.createElement('span');
        ruleName.className = 'fa-new-rule-name';
        ruleName.textContent = FA_RULE_NAMES[rule.name] || rule.name;
        ruleHead.appendChild(ruleName);

        const vote = faNewVoteSummary(rule.vote_details);
        const badge = document.createElement('span');
        badge.className = 'fa-new-rule-badge ' + vote.className;
        badge.textContent = vote.text;
        ruleHead.appendChild(badge);

        put(ruleHead);

        if (rule.part_absent) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = 'КОРПУС НЕ ОБНАРУЖЕН — измерения недоступны';
            put(emptyRow);
            continue;
        }

        // Пороги и замеры этого правила, сгруппированные по объектам.
        const groups = faNewCollectGroups(rule.run_cards);
        if (!groups.general.length && !groups.objects.length) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = rule.skipped ? 'Нет измерений' : 'Нет данных порогов';
            put(emptyRow);
            continue;
        }

        // Общие замеры правила (без привязки к объекту)
        for (const row of groups.general) {
            put(faNewBuildRow(row, isPic));
        }

        // Один объект — один блок со всеми его замерами
        for (const object of groups.objects) {
            const block = document.createElement('div');
            block.className = 'fa-new-obj-block';

            const head = document.createElement('div');
            head.className = 'fa-new-obj-head';
            const name = document.createElement('span');
            name.className = 'fa-new-obj-name';
            name.textContent = object.name;
            head.appendChild(name);

            const status = faNewObjectStatus(object.rows);
            const statusBadge = document.createElement('span');
            statusBadge.className = 'fa-new-obj-badge ' + status.cls;
            statusBadge.textContent = status.text;
            head.appendChild(statusBadge);
            block.appendChild(head);

            for (const row of object.rows) {
                const rowEl = faNewBuildRow(row, isPic);
                const labelEl = rowEl.children[0];
                if (labelEl) {
                    labelEl.textContent = faNewStripObjectPrefix(row.label);
                }
                block.appendChild(rowEl);
            }
            put(block);
        }
    }

    if (frag) {
        tbody.appendChild(frag);
    }
    if (scrollEl) {
        scrollEl.scrollTop = resetScroll ? 0 : keepScroll;
    }
    _faLastScrollContext = scrollContext;
}

// Хук в существующий updateFrameAnalysisStatus
function updateNewFrameAnalysisStatus(ls) {
    const report = ls.frame_analysis || {};
    renderNewFrameAnalysis(report, ls);
}

// Совместимость с тестами и legacy-вызовами: полной панели в HTML нет,
// вызов должен безопасно завершаться.
function renderFrameAnalysisPanel() {
    return null;
}

// Экспорт
if (typeof window !== 'undefined') {
    window.renderNewFrameAnalysis = renderNewFrameAnalysis;
    window.updateNewFrameAnalysisStatus = updateNewFrameAnalysisStatus;
    window.FA_RULE_NAMES = FA_RULE_NAMES;
    window.renderFrameAnalysisPanel = renderFrameAnalysisPanel;
}
