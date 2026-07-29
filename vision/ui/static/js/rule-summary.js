// rule-summary.js — наглядная сводка правила в правой панели
'use strict';

function summaryStateClass(ok) {
    if (ok === true) return 'is-ok';
    if (ok === false) return 'is-bad';
    return 'is-neutral';
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

// Карточка по каждой камере: вердикт, что обнаружено и показатели с допуском.
function renderRuleSummaryCards(cards) {
    const wrap = el('div', 'rule-summary');
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
                cell.append(
                    el('span', 'rule-summary-metric-label', metric.label),
                    el('b', 'rule-summary-metric-value', metric.limit
                        ? `${metric.value} / ${metric.limit}`
                        : metric.value),
                );
                grid.appendChild(cell);
            }
            block.appendChild(grid);
        }
        wrap.appendChild(block);
    }
    return wrap;
}

