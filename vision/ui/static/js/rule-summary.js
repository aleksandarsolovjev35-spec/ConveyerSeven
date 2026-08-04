// rule-summary.js — пороги правил и фактические замеры в анализе кадра
'use strict';

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function measurementClass(run) {
    if (run && run.ok === true) return 'is-ok';
    if (run && run.ok === false) return 'is-bad';
    return 'is-neutral';
}

// Одна строка — один порог: слева его имя, рядом настроенное значение,
// справа — факт именно на открытом оператором кадре. Остальные прогоны
// доступны по раскрытию и не мешают первичному сравнению.
function buildMeasurementRow(roleLabel, metric, pictureRun) {
    const row = el('div', 'fa-measurement-row');
    const label = el('span', 'fa-measurement-label');
    label.textContent = roleLabel
        ? `${roleLabel} · ${metric.label || metric.key || '—'}`
        : (metric.label || metric.key || '—');
    row.appendChild(label);

    const limit = el('span', 'fa-measurement-limit');
    limit.textContent = metric.limit || '—';
    row.appendChild(limit);

    const runs = Array.isArray(metric.runs) ? metric.runs : [];
    const selectedIndex = pictureRun > 0 ? pictureRun - 1 : 0;
    const selectedRun = runs[selectedIndex] || null;
    const current = el('span', `fa-measurement-current ${measurementClass(selectedRun)}`);
    current.textContent = selectedRun && selectedRun.value != null
        ? String(selectedRun.value) : 'нет замера';
    current.title = `Значение на кадре прогона ${selectedIndex + 1}`;
    row.appendChild(current);

    const allRuns = document.createElement('details');
    allRuns.className = 'fa-measurement-all-runs';
    const summary = document.createElement('summary');
    summary.textContent = 'Все прогоны';
    allRuns.appendChild(summary);
    const values = el('span', 'fa-measurement-values');
    runs.forEach((run, index) => {
        const value = el('span', `fa-measurement-value ${measurementClass(run)}`);
        const runNumber = index + 1;
        const measuredValue = run && run.value != null ? String(run.value) : '—';
        value.textContent = `${runNumber}: ${measuredValue}`;
        value.title = `Прогон ${runNumber}: ${measuredValue}`;
        if (pictureRun && runNumber === pictureRun) {
            value.classList.add('is-picture-run');
        }
        values.appendChild(value);
    });
    allRuns.appendChild(values);
    row.appendChild(allRuns);
    return row;
}

function renderRunStatusStrip(rule, pictureRun) {
    const statuses = Array.isArray(rule.run_status) ? rule.run_status : [];
    if (!statuses.length) return null;
    const wrap = el('div', 'fa-run-status');
    statuses.forEach((rows, index) => {
        const chip = el('span', 'fa-run-status-chip');
        const texts = rows.map(row => {
            const role = row.role ? `${cameraRoleLabel(row.role)} · ` : '';
            const reason = row.reason ? ` (${row.reason})` : '';
            return `${role}${row.status || '—'}${reason}`;
        });
        chip.textContent = `ПРОГОН ${index + 1}: ${texts.join(' · ') || '—'}`;
        if (rowStatusClass(rows) === 'is-bad') chip.classList.add('is-bad');
        else if (rowStatusClass(rows) === 'is-ok') chip.classList.add('is-ok');
        if (pictureRun && index + 1 === pictureRun) chip.classList.add('is-picture-run');
        wrap.appendChild(chip);
    });
    return wrap;
}

function rowStatusClass(rows) {
    if (!rows || !rows.length) return 'is-neutral';
    const statuses = rows.map(row => String(row.status || ''));
    if (statuses.some(s => s === 'ОБЛАСТЬ НЕ ПОСТРОЕНА' || s === 'ОТКЛОНЕНИЕ' || s === 'ПУСТО')) return 'is-bad';
    if (statuses.every(s => s === 'В НОРМЕ' || s === 'КОРПУС')) return 'is-ok';
    return 'is-neutral';
}

// В анализе кадра показываются значения замеров: параметр, настроенный
// порог и фактическое значение на текущем кадре (с подсветкой норма/брак).
// Раньше показывались только метрики с порогом — теперь все, чтобы оператор
// видел полную картину: и пороговые, и служебные измерения.
function renderRuleMeasurements(rule, pictureRun) {
    const wrap = el('div', 'fa-measurements');
    const runCards = Array.isArray(rule.run_cards) ? rule.run_cards : [];
    if (runCards.length) {
        wrap.appendChild(el(
            'div', 'fa-measurements-heading',
            `ПАРАМЕТР · ПОРОГ · ЗНАЧЕНИЕ (ПРОГОН ${pictureRun || 1})`,
        ));
    }
    const statusStrip = renderRunStatusStrip(rule, pictureRun);
    if (statusStrip) wrap.appendChild(statusStrip);
    if (!runCards.length) return wrap;

    const byRole = new Map();
    runCards.forEach((cards, runIndex) => {
        for (const card of cards) {
            const role = card.role || '';
            if (!byRole.has(role)) byRole.set(role, new Map());
            const metrics = byRole.get(role);
            for (const metric of card.metrics || []) {
                const key = metric.label || metric.key;
                if (!key) continue;
                if (!metrics.has(key)) {
                    metrics.set(key, {
                        label: metric.label || metric.key,
                        key: metric.key || null,
                        limit: metric.limit || null,
                        runs: runCards.map(() => null),
                    });
                }
                const entry = metrics.get(key);
                if (metric.limit) entry.limit = metric.limit;
                entry.runs[runIndex] = {
                    value: metric.value != null ? metric.value : null,
                    ok: metric.ok == null ? null : metric.ok,
                };
            }
        }
    });

    let hasRows = false;
    for (const [role, metrics] of byRole) {
        const roleLabel = role ? cameraRoleLabel(role) : '';
        for (const metric of metrics.values()) {
            // Показываем все замеры, а не только с порогом — значения нужны
            // оператору для понимания картины (порог может быть «—»).
            wrap.appendChild(buildMeasurementRow(roleLabel, metric, pictureRun));
            hasRows = true;
        }
    }

    // Если по каким-то причинам метрик нет, но есть summary_cards —
    // показать их как fallback, чтобы анализ не был пустым.
    if (!hasRows && Array.isArray(rule.summary_cards)) {
        for (const card of rule.summary_cards) {
            const roleLabel = card.role ? cameraRoleLabel(card.role) : '';
            for (const metric of card.metrics || []) {
                const fakeMetric = {
                    label: metric.label || metric.key || '—',
                    key: metric.key || null,
                    limit: metric.limit || null,
                    runs: [{
                        value: metric.value != null ? metric.value : null,
                        ok: metric.ok == null ? null : metric.ok,
                    }],
                };
                wrap.appendChild(buildMeasurementRow(roleLabel, fakeMetric, 1));
                hasRows = true;
            }
        }
    }

    return wrap;
}
