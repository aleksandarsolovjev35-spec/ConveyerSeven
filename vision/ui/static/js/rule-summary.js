// rule-summary.js — наглядная сводка правила в правой панели
'use strict';

function summaryStateClass(ok) {
    if (ok === true) return 'is-ok';
    if (ok === false) return 'is-bad';
    return 'is-neutral';
}

function summaryResultText(ok) {
    if (ok === true) return 'в допуске';
    if (ok === false) return 'вне порога';
    return '—';
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

// Карточка по каждой камере: вердикт, что обнаружено и показатели с допуском.
// Пороги показаны рядом с результатом (значение / порог — в допуске/вне порога)
// по каждому показателю правила.
function renderRuleSummaryCards(cards) {
    const wrap = el('div', 'rule-summary');
    if (Array.isArray(cards) && cards.some(
        card => Array.isArray(card.metrics) && card.metrics.length,
    )) {
        wrap.appendChild(el('div', 'rule-summary-title', 'ПОРОГИ'));
    }
    for (const card of cards) {
        const block = el('div', `rule-summary-role ${summaryStateClass(card.ok)}`);
        const head = el('div', 'rule-summary-head');
        head.append(
            el('span', 'rule-summary-camera', cameraRoleLabel(card.role)),
            el('span', 'rule-summary-verdict', card.verdict || ''),
        );
        block.appendChild(head);

        const found = Array.isArray(card.found) ? card.found : [];
        if (found.length) {
            block.appendChild(el(
                'div', 'rule-summary-found',
                `Обнаружено — ${found.join(' · ')}`,
            ));
        }

        const metrics = Array.isArray(card.metrics) ? card.metrics : [];
        if (metrics.length) {
            const grid = el('div', 'rule-summary-metrics');
            for (const metric of metrics) {
                const cell = el(
                    'div',
                    `rule-summary-metric ${summaryStateClass(metric.ok)}`,
                );
                const value = el(
                    'b',
                    'rule-summary-metric-value',
                    metric.limit
                        ? `${metric.value} / ${metric.limit}`
                        : metric.value,
                );
                const result = el(
                    'span',
                    'rule-summary-metric-result',
                    summaryResultText(metric.ok),
                );
                cell.append(
                    el('span', 'rule-summary-metric-label', metric.label),
                    value,
                    result,
                );
                grid.appendChild(cell);
            }
            block.appendChild(grid);
        }
        wrap.appendChild(block);
    }
    return wrap;
}

