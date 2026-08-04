// rule-summary.js — иерархия анализа кадра в правой панели:
//
//   Название правила
//     Название порога                         значение порога
//       замер1          замер2          замер3   ← клик = прогон
//       Δ               Δ               Δ
//
// Решающий порог (из-за него сработало правило) выделен.
// Три замера — с трёх прогонов голосования 2 из 3.
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

function formatThresholdValue(metric) {
    if (metric == null) return '—';
    if (metric.limit != null && metric.limit !== '') return String(metric.limit);
    if (typeof metric.limit_raw === 'boolean') {
        return metric.limit_raw ? 'да' : 'нет';
    }
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) {
        return String(metric.limit_raw);
    }
    return '—';
}

function formatRunValue(run) {
    if (!run || run.value == null || run.value === '') return '—';
    if (typeof run.value === 'boolean') return run.value ? 'да' : 'нет';
    return String(run.value);
}

// Δ = value − limit. «+» — выше порога (для «не больше» это обычно брак).
function formatDeltaSimple(run, limitRaw) {
    const valueRaw = run && typeof run.value_raw === 'number' ? run.value_raw : null;
    const limit = typeof limitRaw === 'number' ? limitRaw
        : (run && typeof run.limit_raw === 'number' ? run.limit_raw : null);
    if (valueRaw == null || limit == null) return null;
    if (!Number.isFinite(valueRaw) || !Number.isFinite(limit)) return null;
    const d = valueRaw - limit;
    if (Math.abs(d) < 1e-9) return '0';
    const abs = Math.abs(d);
    const body = abs >= 100 ? String(Math.round(abs))
        : abs >= 10 ? abs.toFixed(1)
        : abs.toFixed(2).replace(/\.?0+$/, '');
    return (d > 0 ? '+' : '−') + body;
}

// Ключи решающих порогов правила (из threshold_breaches).
function decisiveMetricKeys(rule) {
    const keys = new Set();
    const breaches = Array.isArray(rule.threshold_breaches)
        ? rule.threshold_breaches : [];
    for (const b of breaches) {
        if (b && b.label) keys.add(`label:${b.label}`);
        if (b && b.key) keys.add(`key:${b.key}`);
        if (b && b.role && b.label) keys.add(`role:${b.role}|label:${b.label}`);
    }
    return keys;
}

function isMetricDecisive(metric, role, decisiveKeys) {
    if (!decisiveKeys || !decisiveKeys.size) return false;
    if (metric.key && decisiveKeys.has(`key:${metric.key}`)) return true;
    if (metric.label && decisiveKeys.has(`label:${metric.label}`)) return true;
    if (role && metric.label
        && decisiveKeys.has(`role:${role}|label:${metric.label}`)) return true;
    return false;
}

// Одна метрика: название + порог; под ними три замера (+Δ) с data-run.
function buildThresholdBlock(roleLabel, metric, pictureRun, isDecisive) {
    const block = el('div', 'fa-threshold' + (isDecisive ? ' is-decisive' : ''));
    if (isDecisive) block.title = 'Решающий порог — из-за него сработало правило';

    const head = el('div', 'fa-threshold-head');
    const name = el('span', 'fa-threshold-name');
    const baseName = metric.label || metric.key || '—';
    name.textContent = roleLabel ? `${roleLabel} · ${baseName}` : baseName;
    name.title = metric.key || baseName;
    head.appendChild(name);

    const limit = el('span', 'fa-threshold-limit');
    const limitText = formatThresholdValue(metric);
    limit.textContent = limitText;
    limit.title = `Порог: ${limitText}`;
    head.appendChild(limit);
    block.appendChild(head);

    const runsRow = el('div', 'fa-threshold-runs');
    const runs = Array.isArray(metric.runs) ? metric.runs : [];
    const limitRaw = typeof metric.limit_raw === 'number' ? metric.limit_raw : null;

    for (let index = 0; index < 3; index += 1) {
        const run = runs[index] || null;
        const chip = el('span', `fa-measurement-value ${measurementClass(run)}`);
        chip.dataset.run = String(index + 1);
        chip.setAttribute('role', 'button');
        chip.tabIndex = 0;
        chip.title = `Прогон ${index + 1} — показать кадр`;

        const valueSpan = el('span', 'fa-mv-value', formatRunValue(run));
        chip.appendChild(valueSpan);

        const deltaText = formatDeltaSimple(run, limitRaw);
        if (deltaText != null) {
            const deltaSpan = el('span', 'fa-mv-delta', deltaText);
            if (run && run.ok === false) deltaSpan.classList.add('is-bad');
            else if (run && run.ok === true) deltaSpan.classList.add('is-ok');
            chip.appendChild(deltaSpan);
        }

        if (pictureRun && index + 1 === pictureRun) {
            chip.classList.add('is-picture-run');
        }
        runsRow.appendChild(chip);
    }
    block.appendChild(runsRow);
    return block;
}

function rowStatusClass(rows) {
    if (!rows || !rows.length) return 'is-neutral';
    const statuses = rows.map(row => String(row.status || ''));
    if (statuses.some(s => (
        s === 'ОБЛАСТЬ НЕ ПОСТРОЕНА'
        || s === 'ОТКЛОНЕНИЕ'
        || s === 'ПУСТО'
    ))) return 'is-bad';
    if (statuses.every(s => s === 'В НОРМЕ' || s === 'КОРПУС')) return 'is-ok';
    return 'is-neutral';
}

function renderRunStatusStrip(rule, pictureRun) {
    const statuses = Array.isArray(rule.run_status) ? rule.run_status : [];
    if (!statuses.length) return null;
    const wrap = el('div', 'fa-run-status');
    statuses.forEach((rows, index) => {
        const chip = el('span', 'fa-run-status-chip');
        const texts = (rows || []).map(row => {
            const role = row.role ? `${cameraRoleLabel(row.role)} · ` : '';
            const reason = row.reason ? ` (${row.reason})` : '';
            return `${role}${row.status || '—'}${reason}`;
        });
        chip.textContent = `П${index + 1}: ${texts.join(' · ') || '—'}`;
        if (rowStatusClass(rows) === 'is-bad') chip.classList.add('is-bad');
        else if (rowStatusClass(rows) === 'is-ok') chip.classList.add('is-ok');
        if (pictureRun && index + 1 === pictureRun) {
            chip.classList.add('is-picture-run');
        }
        wrap.appendChild(chip);
    });
    return wrap;
}

function collectMetricsByRole(runCards) {
    const byRole = new Map();
    runCards.forEach((cards, runIndex) => {
        for (const card of cards || []) {
            const role = card.role || '';
            if (!byRole.has(role)) byRole.set(role, new Map());
            const metrics = byRole.get(role);
            for (const metric of card.metrics || []) {
                const key = metric.key || metric.label;
                if (!key) continue;
                if (!metrics.has(key)) {
                    metrics.set(key, {
                        label: metric.label || metric.key,
                        key: metric.key || null,
                        limit: metric.limit || null,
                        limit_raw: metric.limit_raw,
                        runs: runCards.map(() => null),
                    });
                }
                const entry = metrics.get(key);
                if (metric.limit != null && metric.limit !== '') {
                    entry.limit = metric.limit;
                }
                if (metric.limit_raw !== undefined) {
                    entry.limit_raw = metric.limit_raw;
                }
                if (metric.label) entry.label = metric.label;
                entry.runs[runIndex] = {
                    value: metric.value != null ? metric.value : null,
                    ok: metric.ok == null ? null : metric.ok,
                    value_raw: typeof metric.value_raw === 'number'
                        ? metric.value_raw : null,
                    limit_raw: typeof metric.limit_raw === 'number'
                        ? metric.limit_raw : null,
                };
            }
        }
    });
    return byRole;
}

function appendSummaryCards(wrap, summaryCards, pictureRun, decisiveKeys) {
    if (!Array.isArray(summaryCards)) return false;
    let added = false;
    for (const card of summaryCards) {
        const role = card.role || '';
        const roleLabel = role ? cameraRoleLabel(role) : '';
        for (const metric of card.metrics || []) {
            const packed = {
                label: metric.label || metric.key || '—',
                key: metric.key || null,
                limit: metric.limit || null,
                limit_raw: metric.limit_raw,
                runs: [{
                    value: metric.value != null ? metric.value : null,
                    ok: metric.ok == null ? null : metric.ok,
                    value_raw: typeof metric.value_raw === 'number'
                        ? metric.value_raw : null,
                    limit_raw: typeof metric.limit_raw === 'number'
                        ? metric.limit_raw : null,
                }, null, null],
            };
            const decisive = isMetricDecisive(packed, role, decisiveKeys);
            wrap.appendChild(buildThresholdBlock(
                roleLabel, packed, pictureRun || 1, decisive,
            ));
            added = true;
        }
    }
    return added;
}

function renderRuleMeasurements(rule, pictureRun) {
    const wrap = el('div', 'fa-measurements');
    const decisiveKeys = rule.triggered ? decisiveMetricKeys(rule) : new Set();

    const statusStrip = renderRunStatusStrip(rule, pictureRun);
    if (statusStrip) wrap.appendChild(statusStrip);

    const runCards = Array.isArray(rule.run_cards) ? rule.run_cards : [];
    if (!runCards.length) {
        appendSummaryCards(wrap, rule.summary_cards, pictureRun, decisiveKeys);
        return wrap;
    }

    const byRole = collectMetricsByRole(runCards);
    let hasRows = false;
    // Решающие пороги — первыми.
    const blocks = [];
    for (const [role, metrics] of byRole) {
        const roleLabel = role ? cameraRoleLabel(role) : '';
        for (const metric of metrics.values()) {
            const decisive = isMetricDecisive(metric, role, decisiveKeys);
            blocks.push({
                decisive,
                node: buildThresholdBlock(roleLabel, metric, pictureRun, decisive),
            });
            hasRows = true;
        }
    }
    blocks.sort((a, b) => Number(b.decisive) - Number(a.decisive));
    for (const b of blocks) wrap.appendChild(b.node);

    if (!hasRows) {
        appendSummaryCards(wrap, rule.summary_cards, pictureRun, decisiveKeys);
    }
    return wrap;
}
