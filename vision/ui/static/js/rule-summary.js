// rule-summary.js — Вся панель видна за 5 секунд: сработавшие сверху, нормальные снизу, всё структурировано
'use strict';

function faEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function faFormatValue(v) {
    if (v == null || v === '') return '—';
    if (typeof v === 'boolean') return v ? 'да' : 'нет';
    return String(v);
}

function faFormatLimit(metric) {
    if (!metric) return '—';
    if (metric.limit != null && metric.limit !== '') return String(metric.limit);
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) return String(metric.limit_raw);
    return '—';
}

function faMeasurementClass(run) {
    if (!run) return 'is-neutral';
    if (run.ok === true) return 'is-ok';
    if (run.ok === false) return 'is-bad';
    return 'is-neutral';
}

function faFormatDelta(run, limitRaw) {
    const valueRaw = run && typeof run.value_raw === 'number' ? run.value_raw : null;
    const limit = typeof limitRaw === 'number' ? limitRaw : (run && typeof run.limit_raw === 'number' ? run.limit_raw : null);
    if (valueRaw == null || limit == null) return null;
    if (!Number.isFinite(valueRaw) || !Number.isFinite(limit)) return null;
    const d = valueRaw - limit;
    if (Math.abs(d) < 1e-9) return '0';
    const abs = Math.abs(d);
    const body = abs >= 100 ? String(Math.round(abs)) : abs >= 10 ? abs.toFixed(1) : abs.toFixed(2).replace(/\.?0+$/, '');
    return (d > 0 ? '+' : '−') + body;
}

function cameraRoleLabel(role) {
    const map = { INPUT_LEFT: 'ВХОД ЛЕВ', INPUT_RIGHT: 'ВХОД ПРАВ', SPIDER_LEFT: 'ПАУК ЛЕВ', SPIDER_RIGHT: 'ПАУК ПРАВ', SPIDER_IN: 'ПАУК ВНУТР', SPIDER_OUT: 'ПАУК НАРУЖ', TOP: 'ВЕРХ' };
    return map[role] || role;
}

const FA_RULE_LABELS = {
    window_geometry: 'Геометрия входа',
    window_sinks: 'Раковины в окнах',
    part_presence: 'Наличие корпуса',
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

const FA_THRESHOLD_LABELS = {
    top_px_min: 'T мин, px', top_px_max: 'T макс, px',
    bottom_px_min: 'B мин, px', bottom_px_max: 'B макс, px',
    top_limits: 'T диапазон, px', bottom_limits: 'B диапазон, px',
    excess_component_min_px: 'Мин. фрагмент, px',
    top_line_max_residual_px: 'Откл. линии, px',
    line_tolerance_px: 'Допуск линии, px',
    omission_tilt_ratio_max: 'Наклон, доля',
    overlap_min_px: 'Мин. пересечение, px',
    max_excess_depth_px: 'Глубина, px',
    largest_component_px: 'Крупн. фрагмент, px',
    max_dev_top: 'Откл. верх, px', max_dev_bottom: 'Откл. низ, px',
    max_dev_height: 'Откл. высоты, px', max_level_slope: 'Наклон, доля',
    rect_width_px: 'Шир. эталона, px', rect_height_px: 'Выс. эталона, px',
    found: 'Контактов, шт', found_raw: 'Контактов (сырые), шт',
    group_L_median_px: 'L медиана, px', group_R_median_px: 'R медиана, px',
    group_T_median_px: 'T медиана, px', group_B_median_px: 'B медиана, px',
    group_L_deviation_px: 'L откл., px', group_R_deviation_px: 'R откл., px',
    group_T_deviation_px: 'T откл., px', group_B_deviation_px: 'B откл., px',
    placement: 'Положение', shift_distance_px: 'Смещение, px', angle_deg: 'Угол, °',
    used_contacts: 'Контактов в обл., шт',
    boundary_width_px: 'Шир. обл., px', boundary_height_px: 'Выс. обл., px',
    sinks_hits: 'Пересечений, шт',
    shell_1_forbidden_px: 'Ракovina #1 запрещ., px', shell_2_forbidden_px: 'Ракovina #2 запрещ., px',
    shell_1_central_px: 'Ракovina #1 центр, px', shell_2_central_px: 'Ракovina #2 центр, px',
    shell_1_platform_px: 'Ракovina #1 платформа, px', shell_2_platform_px: 'Ракovina #2 платформа, px',
    shell_1_contacts_px: 'Ракovina #1 контакты, px', shell_2_contacts_px: 'Ракovina #2 контакты, px',
    glass_hits: 'Совпадений, шт',
    glass_1_platform_px: 'Стекло #1 платформа, px', glass_2_platform_px: 'Стекло #2 платформа, px',
    glass_1_pin_px: 'Стекло #1 пины, px', glass_2_pin_px: 'Стекло #2 пины, px',
    glass_1_ring_px: 'Стекло #1 кольцо, px', glass_2_ring_px: 'Стекло #2 кольцо, px',
    glass_1_union_px: 'Стекло #1 union, px', glass_2_union_px: 'Стекло #2 union, px',
    glass_count: 'Стекол, шт', pins_found: 'Пинов, шт', glass_contact_pairs: 'Пар стекло/контакт, шт',
    glass_1_contact_1_overlap_px: 'С1→К1 перехл., px', glass_1_contact_2_overlap_px: 'С1→К2 перехл., px',
    glass_2_contact_1_overlap_px: 'С2→К1 перехл., px', glass_2_contact_2_overlap_px: 'С2→К2 перехл., px',
};

function faDecisiveKeys(rule) {
    const keys = new Set();
    const breaches = Array.isArray(rule.threshold_breaches) ? rule.threshold_breaches : [];
    for (const b of breaches) { if (!b) continue; if (b.label) keys.add('label:' + b.label); if (b.key) keys.add('key:' + b.key); if (b.role && b.label) keys.add('role:' + b.role + '|label:' + b.label); }
    return keys;
}

function faIsDecisive(metric, role, decisiveKeys) {
    if (!decisiveKeys || !decisiveKeys.size) return false;
    if (metric.key && decisiveKeys.has('key:' + metric.key)) return true;
    if (metric.label && decisiveKeys.has('label:' + metric.label)) return true;
    if (role && metric.label && decisiveKeys.has('role:' + role + '|label:' + metric.label)) return true;
    return false;
}

function faCollectMetrics(runCards) {
    const byRole = new Map();
    const runs = Array.isArray(runCards) ? runCards : [];
    runs.forEach((cards, runIndex) => {
        const list = Array.isArray(cards) ? cards : [];
        for (const card of list) {
            if (!card || typeof card !== 'object') continue;
            const role = card.role || '';
            if (!byRole.has(role)) byRole.set(role, new Map());
            const metrics = byRole.get(role);
            const mList = Array.isArray(card.metrics) ? card.metrics : [];
            for (const metric of mList) {
                if (!metric) continue;
                const key = metric.key || metric.label;
                if (!key) continue;
                if (!metrics.has(key)) metrics.set(key, { label: metric.label || metric.key || '—', key: metric.key || null, limit: metric.limit || null, limit_raw: metric.limit_raw, runs: runs.map(() => null) });
                const entry = metrics.get(key);
                if (metric.limit != null && metric.limit !== '') entry.limit = metric.limit;
                if (metric.limit_raw !== undefined) entry.limit_raw = metric.limit_raw;
                if (metric.label) entry.label = metric.label;
                entry.runs[runIndex] = { value: metric.value != null ? metric.value : null, ok: metric.ok == null ? null : !!metric.ok, value_raw: typeof metric.value_raw === 'number' ? metric.value_raw : null, limit_raw: typeof metric.limit_raw === 'number' ? metric.limit_raw : null };
            }
        }
    });
    return byRole;
}

function faGetObjIndex(label) {
    if (!label) return null;
    let m = label.match(/(?:Окно|Контакт|Раковина|Стекло|Shell)\s*(?:корпуса\s*)?#(\d+)/i);
    if (m) return Number(m[1]);
    m = label.match(/#(\d+)/);
    if (m) return Number(m[1]);
    return null;
}

// ===== ОСНОВНЫЕ ФУНКЦИИ РЕНДЕРА =====

function renderFrameAnalysisPanel(rules, pictureRun, models) {
    const cardsEl = document.getElementById('frame-analysis-cards') || (typeof els !== 'undefined' && els.frameAnalysisCards) || null;
    const titleEl = document.getElementById('frame-analysis-rules-title') || (typeof els !== 'undefined' && els.frameAnalysisRulesTitle) || null;
    const verdictEl = document.getElementById('frame-analysis-verdict') || (typeof els !== 'undefined' && els.frameAnalysisVerdict) || null;
    if (!cardsEl) return;

    const all = Array.isArray(rules) ? rules : [];
    if (titleEl) titleEl.textContent = 'ПРАВИЛА · ' + all.length;

    // Итоговый вердикт
    if (verdictEl) {
        const hasTriggered = all.some(r => r.triggered || r.part_absent);
        const hasSkipped = all.some(r => r.skipped);
        if (hasTriggered) {
            const badRules = all.filter(r => r.triggered).map(r => FA_RULE_LABELS[r.name] || r.name).join(', ');
            verdictEl.textContent = 'БРАК: ' + badRules;
            verdictEl.className = 'fa-verdict bad';
        } else if (hasSkipped) {
            verdictEl.textContent = 'ЕСТЬ ПРОПУЩЕННЫЕ ПРАВИЛА';
            verdictEl.className = 'fa-verdict warn';
        } else {
            verdictEl.textContent = 'ГОДНО';
            verdictEl.className = 'fa-verdict ok';
        }
    }

    // Сортировка: сработавшие → пропущенные → нормальные
    const order = { part_absent: 0, triggered: 1, skipped: 2, ok: 3 };
    const sorted = [...all].sort((a, b) => {
        const oa = a.part_absent ? 0 : (a.triggered ? 1 : (a.skipped ? 2 : 3));
        const ob = b.part_absent ? 0 : (b.triggered ? 1 : (b.skipped ? 2 : 3));
        return oa - ob;
    });

    cardsEl.innerHTML = '';

    // Секция 1: Сработавшие + часть отсутствует
    const badRules = sorted.filter(r => r.part_absent || r.triggered);
    if (badRules.length) {
        cardsEl.appendChild(faEl('div', 'fa-section-title', 'СРАБОТАВШИЕ'));
        badRules.forEach(rule => cardsEl.appendChild(faBuildRuleRow(rule, pictureRun)));
    }

    // Секция 2: Пропущенные
    const skippedRules = sorted.filter(r => r.skipped);
    if (skippedRules.length) {
        cardsEl.appendChild(faEl('div', 'fa-section-title', 'НЕ ВЫПОЛНЕНЫ'));
        skippedRules.forEach(rule => cardsEl.appendChild(faBuildRuleRow(rule, pictureRun)));
    }

    // Секция 3: Нормальные (свернуты в одну строку)
    const okRules = sorted.filter(r => !r.part_absent && !r.triggered && !r.skipped);
    if (okRules.length) {
        cardsEl.appendChild(faEl('div', 'fa-section-title', 'В НОРМЕ · ' + okRules.length));
        const okWrap = faEl('div', 'fa-ok-list');
        okRules.forEach(rule => {
            const row = faEl('div', 'fa-ok-row');
            const vote = rule.vote_details;
            const voteStr = vote ? (vote.decision === 'triggered' ? '🔴' : '🟢') + ' ' + vote.triggered_votes + '/' + vote.total_runs : '—';
            row.innerHTML = '<span class="fa-ok-name">' + (FA_RULE_LABELS[rule.name] || rule.name) + '</span>' +
                           '<span class="fa-ok-vote">' + voteStr + '</span>';
            okWrap.appendChild(row);
        });
        cardsEl.appendChild(okWrap);
    }

    // Model performance — внизу
    if (models && models.length) {
        cardsEl.appendChild(faBuildModelPerformance(models));
    }
}

function faBuildRuleRow(rule, pictureRun) {
    const wrap = faEl('div', 'fa-rule-row' + (rule.part_absent ? ' part-absent' : rule.triggered ? ' triggered' : ''));
    
    // Заголовок правила
    const head = faEl('div', 'fa-rule-head');
    
    const name = faEl('span', 'fa-rule-name', FA_RULE_LABELS[rule.name] || rule.name);
    head.appendChild(name);
    
    const vote = rule.vote_details;
    if (vote) {
        const badge = faEl('span', 'fa-rule-vote' + (vote.decision === 'triggered' ? ' bad' : ' ok'));
        badge.textContent = (vote.decision === 'triggered' ? '🔴' : '🟢') + ' ' + vote.triggered_votes + '/' + vote.total_runs;
        if (vote.picture_run) badge.title = 'Picture: прогон ' + vote.picture_run;
        if (vote.evidence_run) badge.title += (badge.title ? ' | ' : '') + 'Evidence: прогон ' + vote.evidence_run;
        head.appendChild(badge);
    }
    
    if (rule.human_cause && rule.triggered) {
        head.appendChild(faEl('span', 'fa-rule-cause', rule.human_cause));
    }
    
    wrap.appendChild(head);

    // Решающая метрика (одна строка)
    const decisiveKeys = rule.triggered ? faDecisiveKeys(rule) : new Set();
    const runCards = Array.isArray(rule.run_cards) ? rule.run_cards : [];
    const byRole = faCollectMetrics(runCards);
    const blocks = [];
    byRole.forEach((metricsMap, role) => metricsMap.forEach(metric => blocks.push({ metric, role })));
    
    const decisive = faFindDecisiveMetric(blocks, rule.triggered);
    if (decisive) {
        const m = decisive.metric;
        const run = decisive.run;
        const limRaw = typeof m.limit_raw === 'number' ? m.limit_raw : null;
        
        const metricRow = faEl('div', 'fa-rule-metric' + (decisive.isBad ? ' bad' : ''));
        
        const label = faEl('span', 'fa-metric-label', m.label || m.key || '—');
        metricRow.appendChild(label);
        
        const valWrap = faEl('div', 'fa-metric-values');
        const runs = m.runs || [];
        for (let i = 0; i < 3; i++) {
            const r = runs[i] || null;
            const chip = faEl('span', 'fa-metric-chip' + (r && r.ok === false ? ' bad' : r && r.ok === true ? ' ok' : ''));
            chip.textContent = 'П' + (i+1) + ':' + faFormatValue(r ? r.value : '—');
            if (pictureRun && i+1 === pictureRun) chip.classList.add('pic');
            if (vote && vote.evidence_run && i+1 === vote.evidence_run) chip.classList.add('evd');
            valWrap.appendChild(chip);
        }
        if (run && limRaw != null && run.value_raw != null) {
            const delta = faFormatDelta(run, limRaw);
            if (delta) valWrap.appendChild(faEl('span', 'fa-metric-delta' + (run.ok === false ? ' bad' : ''), delta));
        }
        metricRow.appendChild(valWrap);
        
        // Детали по объекту (если есть индекс)
        const objIdx = faGetObjIndex(m.label || '');
        if (objIdx != null) {
            const objInfo = faEl('span', 'fa-metric-obj', 'Объект #' + objIdx);
            metricRow.appendChild(objInfo);
        }
        
        wrap.appendChild(metricRow);
    }

    // Дополнительные метрики (свернуты, клик — раскрыть)
    const extraBlocks = blocks.filter(b => b !== decisive?.block);
    if (extraBlocks.length) {
        const extraBtn = faEl('button', 'fa-extra-toggle', '▸ Ещё ' + extraBlocks.length + ' замеров');
        extraBtn.addEventListener('click', () => {
            if (extraWrap.style.display === 'none') {
                extraWrap.style.display = 'flex';
                extraBtn.textContent = '▾ Скрыть';
            } else {
                extraWrap.style.display = 'none';
                extraBtn.textContent = '▸ Ещё ' + extraBlocks.length + ' замеров';
            }
        });
        wrap.appendChild(extraBtn);
        
        const extraWrap = faEl('div', 'fa-extra-metrics');
        extraWrap.style.display = 'none';
        extraBlocks.forEach(b => {
            extraWrap.appendChild(faBuildThresholdBlock(
                b.role ? cameraRoleLabel(b.role) : '',
                b.metric,
                pictureRun,
                vote?.evidence_run || null,
                faIsDecisive(b.metric, b.role, decisiveKeys)
            ));
        });
        wrap.appendChild(extraWrap);
    }

    // Threshold conclusion
    if (rule.threshold_conclusion) {
        const conc = faEl('div', 'fa-rule-conclusion' + (rule.triggered ? ' bad' : ''));
        conc.textContent = rule.threshold_conclusion;
        wrap.appendChild(conc);
    }

    return wrap;
}

function faFindDecisiveMetric(blocks, isTriggered) {
    let best = null;
    for (const b of blocks) {
        const m = b.metric; if (!m) continue;
        const runs = m.runs || [];
        for (const run of runs) {
            if (!run) continue;
            if (run.ok === false) {
                const delta = Math.abs((run.value_raw || 0) - (m.limit_raw || 0));
                if (!best || delta > best.delta) best = { block: b, delta, run, isBad: true };
            }
        }
    }
    if (!best && !isTriggered) {
        for (const b of blocks) {
            const m = b.metric; if (!m) continue;
            const runs = m.runs || [];
            for (const run of runs) {
                if (!run || run.ok !== true) continue;
                const delta = Math.abs((run.value_raw || 0) - (m.limit_raw || 0));
                if (!best || delta < best.delta) best = { block: b, delta, run, isBad: false };
            }
        }
    }
    return best;
}

// ===== ВСПОМОГАТЕЛЬНЫЕ =====

function faBuildThresholdBlock(roleLabel, metric, pictureRun, evidenceRun, isDecisive) {
    const block = faEl('div', 'fa-thr-item fa-threshold' + (isDecisive ? ' is-decisive' : ''));
    if (isDecisive) block.title = 'Решающий порог';
    const head = faEl('div', 'fa-thr-head');
    const name = faEl('span', 'fa-thr-label fa-threshold-name');
    const dictKey = metric.key || '';
    const niceLabel = metric.label || FA_THRESHOLD_LABELS[dictKey] || metric.key || '—';
    const fullLabel = roleLabel ? (roleLabel + ' · ' + niceLabel) : niceLabel;
    name.textContent = fullLabel;
    head.appendChild(name);
    const limit = faEl('span', 'fa-thr-limit fa-threshold-limit');
    limit.textContent = faFormatLimit(metric);
    head.appendChild(limit);
    block.appendChild(head);
    const valuesRow = faEl('div', 'fa-thr-values fa-threshold-runs');
    const runs = Array.isArray(metric.runs) ? metric.runs : [];
    const limRaw = typeof metric.limit_raw === 'number' ? metric.limit_raw : null;
    for (let i = 0; i < 3; i++) {
        const run = runs[i] || null;
        const chip = faEl('span', 'fa-thr-value fa-measurement-value ' + faMeasurementClass(run));
        chip.setAttribute('data-run', String(i + 1));
        const runLabel = faEl('span', 'fa-mv-run', 'П' + (i + 1));
        chip.appendChild(runLabel);
        const badges = faEl('span', 'fa-mv-badges');
        if (pictureRun && (i + 1) === pictureRun) { badges.appendChild(faEl('span', 'fa-mv-badge fa-mv-badge-picture', 'П')); chip.classList.add('is-picture-run'); }
        if (evidenceRun && (i + 1) === evidenceRun) { badges.appendChild(faEl('span', 'fa-mv-badge fa-mv-badge-evidence', 'Д')); chip.classList.add('is-evidence-run'); }
        chip.appendChild(badges);
        chip.appendChild(faEl('span', 'fa-mv-value', faFormatValue(run ? run.value : null)));
        const deltaText = faFormatDelta(run, limRaw);
        if (deltaText != null) {
            const deltaSpan = faEl('span', 'fa-mv-delta', deltaText);
            if (run && run.ok === false) deltaSpan.classList.add('is-bad');
            else if (run && run.ok === true) deltaSpan.classList.add('is-ok');
            chip.appendChild(deltaSpan);
        }
        valuesRow.appendChild(chip);
    }
    block.appendChild(valuesRow);
    return block;
}

function faBuildModelPerformance(models) {
    if (!models || !Array.isArray(models) || !models.length) return null;
    const wrap = faEl('div', 'fa-model-performance');
    wrap.appendChild(faEl('div', 'fa-model-perf-title', 'Производительность моделей:'));
    const list = faEl('div', 'fa-model-perf-list');
    models.forEach(m => { if (!m || typeof m !== 'object') return; const item = faEl('div', 'fa-model-perf-item'); const header = faEl('div', 'fa-model-perf-header'); header.appendChild(faEl('span', 'fa-model-perf-role', cameraRoleLabel(m.role) + ' · ' + (m.model || 'model'))); if (m.elapsed_ms != null) header.appendChild(faEl('span', 'fa-model-perf-time', m.elapsed_ms.toFixed(1) + ' мс (ср.)')); item.appendChild(header); if (m.detections_by_run && m.detections_by_run.length) { const detRow = faEl('div', 'fa-model-perf-dets'); detRow.textContent = 'Детекции: ' + m.detections_by_run.map((d, i) => 'П' + (i+1) + '=' + d).join(', '); item.appendChild(detRow); } if (m.error) { const errRow = faEl('div', 'fa-model-perf-error'); errRow.textContent = 'Ошибка: ' + m.error; item.appendChild(errRow); } list.appendChild(item); });
    wrap.appendChild(list); return wrap;
}

// ===== ЭКСПОРТ =====

if (typeof window !== 'undefined') {
    window.renderFrameAnalysisPanel = renderFrameAnalysisPanel;
    window.renderFrameAnalysisRules = renderFrameAnalysisPanel;
    window.renderRuleMeasurements = (rule, pr) => { const wrap = faEl('div'); wrap.appendChild(faBuildRuleRow(rule, pr)); return wrap; };
    window.buildFrameAnalysisRuleGroup = (rule, pr) => faBuildRuleRow(rule, pr);
    window.faSyncScroll = () => {};
    window.FA_RULE_LABELS = FA_RULE_LABELS;
}

function cameraRoleLabel(role) {
    const map = { INPUT_LEFT: 'ВХОД ЛЕВ', INPUT_RIGHT: 'ВХОД ПРАВ', SPIDER_LEFT: 'ПАУК ЛЕВ', SPIDER_RIGHT: 'ПАУК ПРАВ', SPIDER_IN: 'ПАУК ВНУТР', SPIDER_OUT: 'ПАУК НАРУЖ', TOP: 'ВЕРХ' };
    return map[role] || role;
}