// rule-summary.js — компактные «три замера порога» в анализе кадра
'use strict';

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

// Строка «порог + три замера» по одной метрике правила. Замер — значение
// метрики в одном из трёх прогонов; вышедший за порог подсвечивается,
// замер выбранного для картинки прогона (pictureRun) помечается рамкой.
function buildMeasurementRow(roleLabel, metric, pictureRun) {
    const row = el('div', 'fa-measurement-row');
    const label = el('span', 'fa-measurement-label');
    label.textContent = roleLabel
        ? `${roleLabel} · ${metric.label || metric.key || '—'}`
        : (metric.label || metric.key || '—');
    row.appendChild(label);

    const limit = el('span', 'fa-measurement-limit');
    limit.textContent = metric.limit
        ? `порог: ${metric.limit}`
        : 'порог: —';
    row.appendChild(limit);

    const values = el('span', 'fa-measurement-values');
    const runs = Array.isArray(metric.runs) ? metric.runs : [];
    runs.forEach((run, index) => {
        const value = el('span', 'fa-measurement-value');
        // Слот фиксирован номером прогона: отсутствующий замер — прочерк,
        // а не сдвиг соседних значений к началу.
        if (run && run.ok === true) value.classList.add('is-ok');
        else if (run && run.ok === false) value.classList.add('is-bad');
        else value.classList.add('is-neutral');
        if (pictureRun && index + 1 === pictureRun) {
            value.classList.add('is-picture-run');
        }
        const runNumber = index + 1;
        const measuredValue = run && run.value != null ? String(run.value) : '—';
        value.textContent = `${runNumber}: ${measuredValue}`;
        value.title = `Прогон ${runNumber}: ${measuredValue}`;
        values.appendChild(value);
    });
    row.appendChild(values);
    return row;
}

// Полоса статусов прогонов: «ОБЛАСТЬ НЕ ПОСТРОЕНА» для fail-closed
// дефектов (omission/стекло без областей), «ОТКЛОНЕНИЕ», «В НОРМЕ».
// Показывается даже когда замеров нет (область не построена).
function renderRunStatusStrip(rule, pictureRun) {
    const statuses = Array.isArray(rule.run_status) ? rule.run_status : [];
    if (!statuses.length) return null;

    const wrap = el('div', 'fa-run-status');
    statuses.forEach((rows, index) => {
        const chip = el('span', 'fa-run-status-chip');
        const texts = rows.map(row => {
            const role = row.role ? `${cameraRoleLabel(row.role)} · ` : '';
            const reason = row.reason
                ? ` (${row.reason})`
                : '';
            return `${role}${row.status || '—'}${reason}`;
        });
        chip.textContent = `ПРОГОН ${index + 1}: ${texts.join(' · ') || '—'}`;
        if (rowStatusClass(rows) === 'is-bad') chip.classList.add('is-bad');
        else if (rowStatusClass(rows) === 'is-ok') chip.classList.add('is-ok');
        if (pictureRun && index + 1 === pictureRun) {
            chip.classList.add('is-picture-run');
        }
        wrap.appendChild(chip);
    });
    return wrap;
}

// Класс статуса прогона: есть «область не построена»/«отклонение» — плохо;
// все «в норме»/«корпус» — хорошо; иначе нейтрально.
function rowStatusClass(rows) {
    if (!rows || !rows.length) return 'is-neutral';
    const statuses = rows.map(row => String(row.status || ''));
    if (statuses.some(s => s === 'ОБЛАСТЬ НЕ ПОСТРОЕНА' || s === 'ОТКЛОНЕНИЕ' || s === 'ПУСТО')) {
        return 'is-bad';
    }
    if (statuses.every(s => s === 'В НОРМЕ' || s === 'КОРПУС')) return 'is-ok';
    return 'is-neutral';
}

// Компактная сводка правила: под названием и вердиктом — все пороги
// правила (labels из «Порогов правил»), под каждым — три замера по
// трём прогонам голосования 2 из 3.
function renderRuleMeasurements(rule, pictureRun) {
    const wrap = el('div', 'fa-measurements');
    const runCards = Array.isArray(rule.run_cards) ? rule.run_cards : [];
    if (runCards.length) {
        wrap.appendChild(el(
            'div',
            'fa-measurements-heading',
            'ПОРОГИ ПРАВИЛА · ЗАМЕРЫ КАДРА ПО ПРОГОНАМ 1 / 2 / 3',
        ));
    }
    const statusStrip = renderRunStatusStrip(rule, pictureRun);
    if (statusStrip) wrap.appendChild(statusStrip);
    if (!runCards.length) return wrap;

    // Собираем метрики по ролям (объединение по названию: у части прогонов
    // метрики может не быть — тогда замер «—»). Замеры раскладываются по
    // слотам номеров прогонов, чтобы не сдвигаться при пропуске.
    const byRole = new Map();
    runCards.forEach((cards, runIndex) => {
        for (const card of cards) {
            const role = card.role || '';
            if (!byRole.has(role)) byRole.set(role, new Map());
            const metrics = byRole.get(role);
            for (const metric of card.metrics || []) {
                const key = metric.label || metric.key;
                if (!metrics.has(key)) {
                    metrics.set(key, {
                        label: metric.label || metric.key,
                        key: metric.key || null,
                        limit: metric.limit || null,
                        // Слоты фиксированы номерами прогонов; явные null,
                        // чтобы forEach не пропускал «дырки» массива.
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

    for (const [role, metrics] of byRole) {
        const roleLabel = role ? cameraRoleLabel(role) : '';
        // Сначала показатели с порогом, затем вспомогательные измерения:
        // так допуск и величина с кадра всегда находятся в начале блока.
        const rows = [...metrics.values()].sort((a, b) => {
            const aHas = a.limit ? 0 : 1;
            const bHas = b.limit ? 0 : 1;
            return aHas - bHas;
        });
        for (const metric of rows) {
            wrap.appendChild(buildMeasurementRow(roleLabel, metric, pictureRun));
        }
    }
    return wrap;
}
