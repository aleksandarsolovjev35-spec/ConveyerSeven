// frame-analysis.js — компактный анализ кадра (правило → порог → лимит → замер)
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

function renderNewFrameAnalysis(report) {
    const panel = document.getElementById('frame-analysis-panel');
    const verdictEl = document.getElementById('fa-new-verdict');
    const contextEl = document.getElementById('fa-new-context');
    const tbody = document.getElementById('fa-new-tbody');

    if (!panel || !tbody) return;
    tbody.innerHTML = '';

    const rules = Array.isArray(report.rules) ? report.rules : [];
    const available = report.available === true;

    if (!available || !rules.length) {
        panel.classList.add('is-collapsed');
        return;
    }
    panel.classList.remove('is-collapsed');

    // Вердикт
    const absent = rules.find(r => r && r.part_absent === true);
    const triggered = rules.filter(r => r && r.triggered === true);
    const skipped = rules.some(r => r && r.skipped === true);

    if (verdictEl) {
        verdictEl.className = 'fa-new-verdict';
        if (absent) {
            verdictEl.textContent = 'КОРПУС НЕ ОБНАРУЖЕН';
            verdictEl.classList.add('warn');
        } else if (triggered.length) {
            const names = triggered.map(r => FA_RULE_NAMES[r.name] || r.name).join(', ');
            verdictEl.textContent = 'БРАК: ' + names;
            verdictEl.classList.add('bad');
        } else if (skipped) {
            verdictEl.textContent = 'ЕСТЬ ПРОПУЩЕННЫЕ ПРАВИЛА';
            verdictEl.classList.add('warn');
        } else {
            verdictEl.textContent = 'ГОДНО';
            verdictEl.classList.add('ok');
        }
    }

    // Контекст
    if (contextEl) {
        const parts = [];
        const stage = String(report.stage || '').toUpperCase();
        if (stage) parts.push(stage);
        if (report.part_id != null) parts.push('КОРПУС #' + report.part_id);
        contextEl.textContent = parts.join(' · ');
    }

    // Сортировка: сработавшие → пропущенные → нормальные
    const sorted = [...rules].sort((a, b) => {
        const order = r => r.part_absent ? 0 : (r.triggered ? 1 : (r.skipped ? 2 : 3));
        return order(a) - order(b);
    });

    const pictureRun = Number(report.picture_run) || 0;

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

        tbody.appendChild(ruleHead);

        if (rule.part_absent) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = 'КОРПУС НЕ ОБНАРУЖЕН — измерения недоступны';
            tbody.appendChild(emptyRow);
            continue;
        }

        // Пороги этого правила
        const thrMap = faNewCollectThresholds(rule.run_cards);
        if (!thrMap.size) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = rule.skipped ? 'Нет измерений' : 'Нет данных порогов';
            tbody.appendChild(emptyRow);
            continue;
        }

        thrMap.forEach((thr) => {
            const row = document.createElement('div');
            row.className = 'fa-new-thr-row';

            // Название порога
            const label = document.createElement('span');
            label.className = 'fa-new-thr-label';
            label.textContent = thr.label;
            row.appendChild(label);

            // Значение порога
            const limit = document.createElement('span');
            limit.className = 'fa-new-thr-limit';
            limit.textContent = faNewFormatLimit(thr);
            row.appendChild(limit);

            // Замер порога (единственный прогон)
            const runs = thr.runs || [];
            const run = runs.length ? runs[0] : null;
            const meas = document.createElement('span');
            const isBad = run && run.ok === false;
            const isOk = run && run.ok === true;
            const isPic = pictureRun === 1;

            meas.className = 'fa-new-meas' +
                (isBad ? ' is-bad' : '') +
                (isOk ? ' is-ok' : '') +
                (isPic ? ' is-pic' : '');

            meas.textContent = faNewFormatValue(run ? run.value : null);
            row.appendChild(meas);

            tbody.appendChild(row);
        });
    }
}

// Хук в существующий updateFrameAnalysisStatus
function updateNewFrameAnalysisStatus(ls) {
    const report = ls.frame_analysis || {};
    renderNewFrameAnalysis(report);
}

// Экспорт
if (typeof window !== 'undefined') {
    window.renderNewFrameAnalysis = renderNewFrameAnalysis;
    window.updateNewFrameAnalysisStatus = updateNewFrameAnalysisStatus;
    window.FA_RULE_NAMES = FA_RULE_NAMES;
}
