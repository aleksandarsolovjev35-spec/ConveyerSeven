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
function faCameraRoleLabel(role) {
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
    shell_1_forbidden_px: 'Раковина #1 запрещ., px', shell_2_forbidden_px: 'Раковина #2 запрещ., px',
    shell_1_central_px: 'Раковина #1 центр, px', shell_2_central_px: 'Раковина #2 центр, px',
    shell_1_platform_px: 'Раковина #1 платформа, px', shell_2_platform_px: 'Раковина #2 платформа, px',
    shell_1_contacts_px: 'Раковина #1 контакты, px', shell_2_contacts_px: 'Раковина #2 контакты, px',
    glass_hits: 'Совпадений, шт',
    glass_1_platform_px: 'Стекло #1 платформа, px', glass_2_platform_px: 'Стекло #2 платформа, px',
    glass_1_pin_px: 'Стекло #1 пины, px', glass_2_pin_px: 'Стекло #2 пины, px',
    glass_1_ring_px: 'Стекло #1 кольцо, px', glass_2_ring_px: 'Стекло #2 кольцо, px',
    glass_1_union_px: 'Стекло #1 union, px', glass_2_union_px: 'Стекло #2 union, px',
    glass_count: 'Стекол, шт', pins_found: 'Пинов, шт', glass_contact_pairs: 'Пар стекло/контакт, шт',
    glass_1_contact_1_overlap_px: 'С1→К1 перехл., px', glass_1_contact_2_overlap_px: 'С1→К2 перехл., px',
    glass_2_contact_1_overlap_px: 'С2→К1 перехл., px', glass_2_contact_2_overlap_px: 'С2→К2 перехл., px',
};
let faDrag = null;
let faScrollBound = false;
function faScrollElements() {
    const cards = document.getElementById('frame-analysis-cards')
        || (typeof els !== 'undefined' && els.frameAnalysisCards);
    const track = document.getElementById('frame-analysis-scroll-track')
        || document.querySelector('#frame-analysis-body .fa-scroll-track');
    const thumb = track && track.querySelector('.fa-scroll-thumb');
    return {cards, track, thumb};
}
function faClamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}
function faSyncScroll() {
    const {cards, track, thumb} = faScrollElements();
    if (!cards || !track || !thumb) return;
    const maxScroll = Math.max(0, cards.scrollHeight - cards.clientHeight);
    if (maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        thumb.style.height = '';
        if (cards.scrollTop) cards.scrollTop = 0;
        return;
    }
    track.classList.remove('is-idle');
    if (faDrag) return;
    const trackHeight = track.clientHeight || 56;
    const ratio = cards.clientHeight / Math.max(1, cards.scrollHeight);
    const thumbHeight = Math.max(
        22,
        Math.min(Math.round(trackHeight * 0.6), Math.round(trackHeight * ratio)),
    );
    const maxThumbTop = Math.max(0, trackHeight - thumbHeight);
    const top = maxScroll > 0
        ? (cards.scrollTop / maxScroll) * maxThumbTop
        : 0;
    thumb.style.height = `${thumbHeight}px`;
    thumb.style.top = `${top}px`;
}
function setupFrameAnalysisScroll() {
    if (faScrollBound) return;
    const {cards, track, thumb} = faScrollElements();
    if (!cards || !track || !thumb) return;
    faScrollBound = true;
    cards.addEventListener('scroll', () => {
        if (!faDrag) faSyncScroll();
    });
    const stopDrag = () => {
        if (!faDrag) return;
        faDrag.track.classList.remove('is-dragging');
        faDrag.thumb.style.transition = '';
        faDrag = null;
        document.removeEventListener('mousemove', moveDrag);
        document.removeEventListener('mouseup', stopDrag);
        faSyncScroll();
    };
    const moveDrag = (event) => {
        if (!faDrag) return;
        const {cards: activeCards, thumb: activeThumb, startY,
            startTop, maxScroll, maxThumbTop} = faDrag;
        const top = faClamp(
            startTop + event.clientY - startY,
            0,
            maxThumbTop,
        );
        activeThumb.style.top = `${top}px`;
        activeCards.scrollTop = maxThumbTop > 0
            ? (top / maxThumbTop) * maxScroll
            : 0;
    };
    track.addEventListener('mousedown', event => {
        if (event.target === thumb || track.classList.contains('is-idle')) return;
        const maxScroll = Math.max(0, cards.scrollHeight - cards.clientHeight);
        if (maxScroll <= 0) return;
        const trackRect = track.getBoundingClientRect();
        const thumbHeight = thumb.offsetHeight || 22;
        const maxThumbTop = Math.max(0, track.clientHeight - thumbHeight);
        const top = faClamp(
            event.clientY - trackRect.top - thumbHeight / 2,
            0,
            maxThumbTop,
        );
        thumb.style.transition = 'none';
        thumb.style.top = `${top}px`;
        cards.scrollTop = maxThumbTop > 0
            ? (top / maxThumbTop) * maxScroll
            : 0;
        if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => { thumb.style.transition = ''; });
        else thumb.style.transition = '';
    });
    thumb.addEventListener('mousedown', event => {
        if (track.classList.contains('is-idle')) return;
        event.preventDefault();
        event.stopPropagation();
        const maxScroll = Math.max(0, cards.scrollHeight - cards.clientHeight);
        const thumbHeight = thumb.offsetHeight || 22;
        const maxThumbTop = Math.max(0, track.clientHeight - thumbHeight);
        faDrag = {
            cards,
            track,
            thumb,
            startY: event.clientY,
            startTop: parseFloat(thumb.style.top) || 0,
            maxScroll,
            maxThumbTop,
        };
        track.classList.add('is-dragging');
        thumb.style.transition = 'none';
        document.addEventListener('mousemove', moveDrag);
        document.addEventListener('mouseup', stopDrag);
    });
    track.addEventListener('wheel', event => {
        if (track.classList.contains('is-idle') || faDrag) return;
        event.preventDefault();
        cards.scrollTop += event.deltaY;
    }, {passive: false});
    if (typeof window !== 'undefined') {
        window.addEventListener('resize', faSyncScroll);
    }
    faSyncScroll();
}
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
function faVoteSummary(vote) {
    if (!vote) return {className: 'ok', text: '—'};
    const total = Number(vote.total_runs) || 3;
    // Тройное голосование убрано: при одном прогоне счётчик не показываем.
    const single = total <= 1;
    const count = (value) => single ? '' : ` · ${value ?? 0}/${total}`;
    if (vote.decision === 'empty') {
        return {
            className: 'warn',
            text: `🟡 ПУСТО${count(vote.empty_votes ?? vote.triggered_votes)}`,
        };
    }
    if (vote.decision === 'present') {
        return {
            className: 'ok',
            text: `🟢 КОРПУС${count(vote.present_votes ?? vote.normal_votes)}`,
        };
    }
    if (vote.decision === 'triggered') {
        return {
            className: 'bad',
            text: `🔴 СРАБОТАЛО${count(vote.triggered_votes)}`,
        };
    }
    return {
        className: 'ok',
        text: `🟢 НОРМА${count(vote.normal_votes)}`,
    };
}
// ===== ОСНОВНЫЕ ФУНКЦИИ РЕНДЕРА =====
function renderFrameAnalysisPanel(rules, pictureRun, models, meta = {}) {
    const cardsEl = document.getElementById('frame-analysis-cards')
        || (typeof els !== 'undefined' && els.frameAnalysisCards) || null;
    const titleEl = document.getElementById('frame-analysis-rules-title')
        || (typeof els !== 'undefined' && els.frameAnalysisRulesTitle) || null;
    const verdictEl = document.getElementById('frame-analysis-verdict')
        || (typeof els !== 'undefined' && els.frameAnalysisVerdict) || null;
    if (!cardsEl) return;
    setupFrameAnalysisScroll();
    const all = Array.isArray(rules) ? rules : [];
    const totalRules = Number.isFinite(Number(meta.totalRules))
        ? Number(meta.totalRules)
        : all.length;
    if (titleEl) {
        const suffix = totalRules !== all.length
            ? `${all.length}/${totalRules}`
            : String(all.length);
        titleEl.textContent = 'ПРАВИЛА · ' + suffix;
    }
    const status = String(meta.status || '').toUpperCase();
    const message = String(meta.message || '').trim();
    const absent = all.find(rule => rule && rule.part_absent === true);
    const triggered = all.filter(rule => rule && rule.triggered === true);
    const skipped = all.some(rule => rule && rule.skipped === true);
    // Итог должен описывать данные, а не отфильтрованный пустой список.
    // Раньше part_absent превращался в «БРАК: » (пустая причина), а пустой
    // список во время RUNNING ошибочно показывался как «ГОДНО».
    if (verdictEl) {
        if (status === 'ERROR') {
            verdictEl.textContent = message || 'ОШИБКА АНАЛИЗА';
            verdictEl.className = 'fa-verdict bad';
        } else if (!all.length) {
            verdictEl.textContent = status === 'RUNNING'
                ? 'ПОДГОТОВКА МОДЕЛЕЙ И ПРАВИЛ'
                : (message || 'НЕТ РЕЗУЛЬТАТОВ');
            verdictEl.className = 'fa-verdict warn';
        } else if (absent) {
            verdictEl.textContent = 'КОРПУС НЕ ОБНАРУЖЕН';
            verdictEl.className = 'fa-verdict warn';
        } else if (triggered.length) {
            const badRules = triggered
                .map(rule => FA_RULE_LABELS[rule.name] || rule.name)
                .join(', ');
            verdictEl.textContent = 'БРАК: ' + badRules;
            verdictEl.className = 'fa-verdict bad';
        } else if (skipped) {
            verdictEl.textContent = 'ЕСТЬ ПРОПУЩЕННЫЕ ПРАВИЛА';
            verdictEl.className = 'fa-verdict warn';
        } else {
            verdictEl.textContent = 'ГОДНО';
            verdictEl.className = 'fa-verdict ok';
        }
    }
    // Сортировка: сработавшие → пропущенные → нормальные.
    const sorted = [...all].sort((a, b) => {
        const order = rule => rule.part_absent
            ? 0 : (rule.triggered ? 1 : (rule.skipped ? 2 : 3));
        return order(a) - order(b);
    });
    cardsEl.innerHTML = '';
    if (!all.length) {
        cardsEl.appendChild(faEl(
            'div',
            status === 'ERROR' ? 'fa-empty fa-empty-error' : 'fa-empty',
            status === 'ERROR'
                ? (message || 'Анализ завершился с ошибкой')
                : (message || 'Ожидание результатов'),
        ));
    }
    // Секция 1: сработавшие + отсутствующий корпус.
    const badRules = sorted.filter(rule => rule.part_absent || rule.triggered);
    if (badRules.length) {
        cardsEl.appendChild(faEl('div', 'fa-section-title', 'СРАБОТАВШИЕ'));
        badRules.forEach(rule => {
            cardsEl.appendChild(faBuildRuleRow(rule, pictureRun));
        });
    }
    // Секция 2: пропущенные.
    const skippedRules = sorted.filter(rule => rule.skipped);
    if (skippedRules.length) {
        cardsEl.appendChild(faEl('div', 'fa-section-title', 'НЕ ВЫПОЛНЕНЫ'));
        skippedRules.forEach(rule => {
            cardsEl.appendChild(faBuildRuleRow(rule, pictureRun));
        });
    }
    // Секция 3: нормальные правила — компактный список.
    const okRules = sorted.filter(
        rule => !rule.part_absent && !rule.triggered && !rule.skipped,
    );
    if (okRules.length) {
        cardsEl.appendChild(faEl(
            'div', 'fa-section-title', 'В НОРМЕ · ' + okRules.length,
        ));
        const okWrap = faEl('div', 'fa-ok-list');
        okRules.forEach(rule => {
            const row = faEl('div', 'fa-ok-row');
            const name = faEl(
                'span', 'fa-ok-name', FA_RULE_LABELS[rule.name] || rule.name,
            );
            const vote = faVoteSummary(rule.vote_details);
            row.appendChild(name);
            const voteEl = faEl('span', 'fa-ok-vote ' + vote.className, vote.text);
            okWrap.appendChild(row);
            row.appendChild(voteEl);
        });
        cardsEl.appendChild(okWrap);
    }
    // Производительность моделей — внизу списка.
    if (Array.isArray(models) && models.length) {
        const performance = faBuildModelPerformance(models);
        if (performance) cardsEl.appendChild(performance);
    }
    // Recalculate after the new DOM has received its layout.
    if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(faSyncScroll);
    } else {
        faSyncScroll();
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
        const summary = faVoteSummary(vote);
        const badge = faEl('span', 'fa-rule-vote ' + summary.className, summary.text);
        // Тройное голосование убрано: детали прогонов в tooltip не нужны.
        head.appendChild(badge);
    }
    if (rule.human_cause && rule.triggered) {
        head.appendChild(faEl('span', 'fa-rule-cause', rule.human_cause));
    }
    wrap.appendChild(head);
    if (rule.part_absent) {
        wrap.appendChild(faEl('div', 'fa-empty-status', 'КОРПУС НЕ ОБНАРУЖЕН'));
    }
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
        const singleRun = runs.length <= 1;
        const runCount = singleRun ? 1 : runs.length;
        for (let i = 0; i < runCount; i++) {
            const r = runs[i] || null;
            const chip = faEl('span', 'fa-metric-chip' + (r && r.ok === false ? ' bad' : r && r.ok === true ? ' ok' : ''));
            chip.textContent = singleRun
                ? faFormatValue(r ? r.value : '—')
                : 'П' + (i+1) + ':' + faFormatValue(r ? r.value : '—');
            if (!singleRun) {
                chip.dataset.run = String(i + 1);
                chip.setAttribute('role', 'button');
                chip.tabIndex = 0;
                chip.title = 'Прогон ' + (i + 1) + ' — показать кадр';
            }
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
                b.role ? faCameraRoleLabel(b.role) : '',
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
                if (!best || delta < best.delta) best = { block: b, metric: m, delta, run, isBad: true };
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
                if (!best || delta < best.delta) best = { block: b, metric: m, delta, run, isBad: false };
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
    const singleRun = runs.length <= 1;
    const runCount = singleRun ? 1 : runs.length;
    const limRaw = typeof metric.limit_raw === 'number' ? metric.limit_raw : null;
    for (let i = 0; i < runCount; i++) {
        const run = runs[i] || null;
        const chip = faEl('span', 'fa-thr-value fa-measurement-value ' + faMeasurementClass(run));
        if (!singleRun) {
            chip.setAttribute('data-run', String(i + 1));
            chip.setAttribute('role', 'button');
            chip.tabIndex = 0;
            chip.title = 'Прогон ' + (i + 1) + ' — показать кадр';
        }
        if (!singleRun) {
            const runLabel = faEl('span', 'fa-mv-run', 'П' + (i + 1));
            chip.appendChild(runLabel);
        }
        const badges = faEl('span', 'fa-mv-badges');
        if (!singleRun) {
            if (pictureRun && (i + 1) === pictureRun) { badges.appendChild(faEl('span', 'fa-mv-badge fa-mv-badge-picture', 'П')); chip.classList.add('is-picture-run'); }
            if (evidenceRun && (i + 1) === evidenceRun) { badges.appendChild(faEl('span', 'fa-mv-badge fa-mv-badge-evidence', 'Д')); chip.classList.add('is-evidence-run'); }
        }
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
    models.forEach(m => { if (!m || typeof m !== 'object') return; const item = faEl('div', 'fa-model-perf-item'); const header = faEl('div', 'fa-model-perf-header'); header.appendChild(faEl('span', 'fa-model-perf-role', faCameraRoleLabel(m.role) + ' · ' + (m.model || 'model'))); if (m.elapsed_ms != null && Number.isFinite(Number(m.elapsed_ms))) header.appendChild(faEl('span', 'fa-model-perf-time', Number(m.elapsed_ms).toFixed(1) + ' мс (ср.)')); item.appendChild(header); if (m.detections_by_run && m.detections_by_run.length) { const detRow = faEl('div', 'fa-model-perf-dets'); detRow.textContent = m.detections_by_run.length <= 1 ? 'Детекции: ' + m.detections_by_run[0] : 'Детекции: ' + m.detections_by_run.map((d, i) => 'П' + (i+1) + '=' + d).join(', '); item.appendChild(detRow); } if (m.error) { const errRow = faEl('div', 'fa-model-perf-error'); errRow.textContent = 'Ошибка: ' + m.error; item.appendChild(errRow); } list.appendChild(item); });
    wrap.appendChild(list); return wrap;
}
// ===== ЭКСПОРТ =====
if (typeof window !== 'undefined') {
    window.renderFrameAnalysisPanel = renderFrameAnalysisPanel;
    window.renderFrameAnalysisRules = renderFrameAnalysisPanel;
    window.renderRuleMeasurements = (rule, pr) => { const wrap = faEl('div'); wrap.appendChild(faBuildRuleRow(rule, pr)); return wrap; };
    window.buildFrameAnalysisRuleGroup = (rule, pr) => faBuildRuleRow(rule, pr);
    window.faSyncScroll = faSyncScroll;
    window.FA_RULE_LABELS = FA_RULE_LABELS;
}
