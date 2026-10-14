'use strict';

const token = document.querySelector('meta[name="panel-token"]').content;
const POLL_MS = 3000;
const ESTIMATE_DELAY_MS = 300;
const FOCUS_LABELS = {security: 'Segurança', quality: 'Qualidade', all: 'Segurança e qualidade'};
const STATUS_LABELS = {
    queued: 'na fila', running: 'rodando', done: 'concluída', failed: 'falhou',
    inconclusive: 'inconclusiva (cota)', cancelled: 'cancelada', lost: 'perdida',
};
const STATUS_TONE = {
    queued: '', running: 'warn', done: 'good', failed: 'bad', inconclusive: 'warn',
    cancelled: '', lost: 'bad',
};

const state = {plugins: [], staticSummary: {}, logs: {}, current: null};

const $ = (id) => document.getElementById(id);

/** Builds an element; strings become text nodes, never markup. */
const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'class') {
            node.className = value;
        } else if (key.startsWith('on')) {
            node.addEventListener(key.slice(2), value);
        } else if (value !== false && value !== null && value !== undefined) {
            node.setAttribute(key, value === true ? '' : value);
        }
    }
    for (const child of children.flat()) {
        if (child !== null && child !== undefined && child !== false) {
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
    }
    return node;
};

const api = async (path, {method = 'GET', body} = {}) => {
    const headers = {};
    if (method !== 'GET') {
        headers['Content-Type'] = 'application/json';
        headers['X-Panel-Token'] = token;
    }
    const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
    const isJson = (response.headers.get('Content-Type') || '').includes('json');
    const data = isJson ? await response.json() : await response.text();
    if (!response.ok) {
        throw new Error((data && data.error) || response.statusText);
    }
    return data;
};

const gradeTone = (grade) => {
    if (['A+', 'A'].includes(grade)) {
        return 'good';
    }
    if (['B+', 'B'].includes(grade)) {
        return 'warn';
    }
    return ['C', 'D', 'F'].includes(grade) ? 'bad' : '';
};

const chip = (text, tone = '') => h('span', {class: `chip ${tone}`.trim()}, text);

const formatDate = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString('pt-BR') : '—');

const formatDuration = (seconds) => {
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    return minutes ? `${minutes}min ${total % 60}s` : `${total}s`;
};

// ---- plugins ------------------------------------------------------------- //

const auditCell = (audit) => {
    if (!audit) {
        return h('span', {class: 'hint'}, 'nunca');
    }
    const chips = [];
    if (audit.focus === 'security') {
        chips.push(chip(`Seg ${audit.grade}`, gradeTone(audit.grade)));
    } else if (audit.focus === 'quality') {
        chips.push(chip(`Qual ${audit.grade}`, gradeTone(audit.grade)));
    } else {
        chips.push(chip(`Geral ${audit.grade}`, gradeTone(audit.grade)));
        if (audit.security_grade) {
            chips.push(chip(`Seg ${audit.security_grade}`, gradeTone(audit.security_grade)));
        }
    }
    if (audit.partial) {
        chips.push(chip('parcial'));
    }
    return h('div', {}, chips, h('small', {}, audit.date));
};

const staticCell = (plugin) => {
    const summary = state.staticSummary[plugin.path];
    if (summary === undefined) {
        return h('span', {class: 'hint'}, '—');
    }
    if (summary.error) {
        return chip('erro', 'bad');
    }
    const count = Object.values(summary).reduce((sum, n) => sum + n, 0);
    return count === 0
        ? chip('limpo', 'good')
        : h('button', {type: 'button', class: 'chip bad', onclick: () => showStatic(plugin)},
            `${count} ocorrência(s)`);
};

const renderPlugins = () => {
    const body = document.querySelector('#plugins tbody');
    body.replaceChildren(...state.plugins.map((plugin) => h('tr', {},
        h('td', {}, h('strong', {}, plugin.component), h('small', {}, plugin.path)),
        h('td', {}, plugin.release || plugin.version),
        h('td', {}, auditCell(plugin.last_audit)),
        h('td', {}, staticCell(plugin)),
        h('td', {},
            h('button', {type: 'button', class: 'btn', onclick: () => openRunDialog(plugin)}, 'Auditar'),
            h('button', {type: 'button', class: 'btn', onclick: () => showStatic(plugin)}, 'Checar'),
            h('button', {type: 'button', class: 'btn', onclick: () => openReports(plugin)}, 'Relatórios')),
    )));
};

const loadPlugins = async () => {
    state.plugins = await api('/api/plugins');
    renderPlugins();
};

const checkAll = async () => {
    const button = $('check-all');
    button.disabled = true;
    button.textContent = 'Checando…';
    try {
        state.staticSummary = await api('/api/static-all');
        renderPlugins();
    } finally {
        button.disabled = false;
        button.textContent = 'Checar todos (determinístico)';
    }
};

const showStatic = async (plugin) => {
    $('static-title').textContent = `Checagens — ${plugin.component}`;
    const body = $('static-body');
    body.replaceChildren(h('p', {class: 'hint'}, 'Rodando…'));
    $('static-dialog').showModal();
    try {
        const {results} = await api(`/api/static?plugin=${encodeURIComponent(plugin.path)}`);
        state.staticSummary[plugin.path] = Object.fromEntries(
            Object.entries(results).filter(([, list]) => list.length).map(([k, v]) => [k, v.length]));
        renderPlugins();
        const groups = Object.entries(results).filter(([, list]) => list.length);
        body.replaceChildren(...(groups.length
            ? groups.map(([name, list]) => h('div', {},
                h('h3', {}, name),
                h('ul', {class: 'static-list'}, list.map((v) => h('li', {}, h('code', {}, v.text))))))
            : [h('p', {}, 'Nenhuma ocorrência.')]));
    } catch (error) {
        body.replaceChildren(h('p', {class: 'error', role: 'alert'}, error.message));
    }
};

// ---- run dialog ---------------------------------------------------------- //

let estimateTimer = null;

const runOptions = () => {
    const form = $('run-form');
    const scope = form.elements.scope.value;
    return {
        plugin: state.current.path,
        focus: form.elements.focus.value,
        only: [...document.querySelectorAll('#groups input:checked')].map((box) => box.value),
        since: scope === 'since' ? $('since').value : null,
        full: scope === 'full',
        with_tests: $('with-tests').checked,
        no_verify: $('no-verify').checked,
    };
};

const syncRunForm = () => {
    const focus = $('run-form').elements.focus.value;
    $('full-choice').hidden = focus !== 'quality';
    if (focus !== 'quality' && $('run-form').elements.scope.value === 'full') {
        $('run-form').elements.scope.value = 'default';
    }
    $('scope-default-label').textContent = focus === 'quality'
        ? 'Padrão: o que mudou desde a última tag'
        : 'Padrão: o plugin (ou os grupos marcados)';
    $('since').disabled = $('run-form').elements.scope.value !== 'since';
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(updateEstimate, ESTIMATE_DELAY_MS);
};

const updateEstimate = async () => {
    const output = $('estimate');
    const options = runOptions();
    const query = new URLSearchParams({
        plugin: options.plugin, focus: options.focus, only: options.only.join(','),
        since: options.since || '', full: options.full ? '1' : '0',
        with_tests: options.with_tests ? '1' : '0',
    });
    try {
        const data = await api(`/api/estimate?${query}`);
        const parts = [`Lê ${data.files} arquivos, ${data.lines} linhas, cerca de ${data.batches} chamada(s) de IA`
            + ' na varredura (mais as de verificação).'];
        if (data.note) {
            parts.push(data.note.charAt(0).toUpperCase() + data.note.slice(1) + '.');
        }
        if (data.files === 0) {
            parts.push('Nada a analisar neste escopo.');
        }
        output.textContent = parts.join(' ');
    } catch (error) {
        output.textContent = `Estimativa indisponível: ${error.message}`;
    }
};

const openRunDialog = async (plugin) => {
    state.current = plugin;
    $('run-title').textContent = `Auditar ${plugin.component}`;
    $('run-error').hidden = true;
    $('run-form').reset();
    $('estimate').textContent = 'Calculando estimativa…';
    $('run-dialog').showModal();
    const detail = await api(`/api/plugin?path=${encodeURIComponent(plugin.path)}`);
    const groups = $('groups');
    groups.replaceChildren(...(detail.groups.length
        ? detail.groups.map((group) => h('label', {class: 'check'},
            h('input', {type: 'checkbox', value: group.name, onchange: syncRunForm}),
            ` ${group.name} — ${group.files} arquivos, ${group.lines} linhas`,
            group.tests ? chip('lê tests/') : null,
            group.closed ? chip('segurança encerrada', 'good') : null))
        : [h('p', {class: 'hint'}, 'Sem grupos configurados: lê o plugin inteiro.')]));
    $('since').replaceChildren(...(detail.tags.length
        ? detail.tags.map((tag) => h('option', {value: tag}, tag))
        : [h('option', {value: ''}, 'sem tags')]));
    syncRunForm();
};

const submitRun = async (event) => {
    event.preventDefault();
    const error = $('run-error');
    error.hidden = true;
    $('run-submit').disabled = true;
    try {
        await api('/api/runs', {method: 'POST', body: runOptions()});
        $('run-dialog').close();
        await loadRuns();
        $('h-runs').scrollIntoView({behavior: 'smooth'});
    } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
    } finally {
        $('run-submit').disabled = false;
    }
};

// ---- runs ---------------------------------------------------------------- //

const runTitle = (run) => {
    const groups = run.options.only.length ? ` · grupos ${run.options.only.join(', ')}` : '';
    return `${run.options.component} — ${FOCUS_LABELS[run.options.focus]}${groups}`;
};

const loadLog = async (run) => {
    const log = state.logs[run.id] || (state.logs[run.id] = {text: '', offset: 0});
    const data = await api(`/api/runs/${run.id}/log?offset=${log.offset}`);
    log.text += data.text;
    log.offset = data.offset;
    return log.text;
};

const progressLine = async (run) => {
    const progress = await api(`/api/progress?component=${encodeURIComponent(run.options.component)}`);
    if (!progress.phase || progress.finished) {
        return '';
    }
    const done = progress.done !== null && progress.total ? ` (${progress.done}/${progress.total})` : '';
    return `Fase ${progress.phase}: ${progress.description}${done}`;
};

const runCard = async (run) => {
    const meta = [`criada ${formatDate(run.created)}`];
    if (run.started) {
        const end = run.finished || Date.now() / 1000;
        meta.push(`duração ${formatDuration(end - run.started)}`);
    }
    const active = ['queued', 'running'].includes(run.status);
    const actions = [];
    if (active) {
        actions.push(h('button', {type: 'button', class: 'btn', onclick: () => cancelRun(run.id)}, 'Cancelar'));
    }
    if (run.report) {
        const plugin = state.plugins.find((p) => p.path === run.options.plugin);
        actions.push(h('button', {type: 'button', class: 'btn',
            onclick: () => openReports(plugin, run.report)}, 'Ver relatório'));
    }
    const progress = run.status === 'running' ? await progressLine(run) : '';
    const logText = ['running', 'done', 'failed', 'inconclusive', 'lost'].includes(run.status)
        ? await loadLog(run) : '';
    const details = h('details', {},
        h('summary', {}, 'Saída'),
        h('pre', {}, logText.slice(-6000) || '(vazia)'));
    return h('div', {class: 'run'},
        h('div', {class: 'run-head'}, h('strong', {}, runTitle(run)),
            chip(STATUS_LABELS[run.status] || run.status, STATUS_TONE[run.status])),
        h('div', {class: 'meta'}, meta.join(' · ')),
        progress ? h('div', {class: 'progress'}, progress) : null,
        run.status === 'inconclusive'
            ? h('div', {class: 'meta'}, 'A cota acabou: o que foi lido ficou em cache, rode de novo depois.') : null,
        logText || active ? details : null,
        actions.length ? h('div', {class: 'row-actions'}, actions) : null);
};

const loadRuns = async () => {
    const runs = await api('/api/runs');
    const active = runs.filter((run) => run.status === 'running');
    const queued = runs.filter((run) => run.status === 'queued');
    $('queue-status').textContent = active.length
        ? `Rodando: ${active[0].options.component}${queued.length ? ` · ${queued.length} na fila` : ''}`
        : (queued.length ? `${queued.length} na fila` : 'Ocioso');
    if (!runs.length) {
        $('runs').replaceChildren(h('p', {class: 'hint'}, 'Nenhuma execução ainda.'));
        return;
    }
    const open = new Set([...document.querySelectorAll('#runs details[open]')]
        .map((node) => node.closest('.run').dataset.id));
    const cards = await Promise.all(runs.map(runCard));
    cards.forEach((card, index) => {
        card.dataset.id = runs[index].id;
        if (open.has(runs[index].id)) {
            const details = card.querySelector('details');
            if (details) {
                details.open = true;
            }
        }
    });
    $('runs').replaceChildren(...cards);
};

const cancelRun = async (id) => {
    await api(`/api/runs/${id}/cancel`, {method: 'POST', body: {}});
    await loadRuns();
};

// ---- reports ------------------------------------------------------------- //

const escapeHtml = (text) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/** Inline Markdown on text that is escaped first, so nothing in a report can become markup. */
const inline = (text) => escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');

const isTableSeparator = (line) => /^\|[\s:|-]+\|\s*$/.test(line || '');
const splitRow = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());

const renderMarkdown = (source) => {
    const lines = source.split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        if (line.startsWith('```')) {
            const code = [];
            i += 1;
            while (i < lines.length && !lines[i].startsWith('```')) {
                code.push(lines[i]);
                i += 1;
            }
            i += 1;
            out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
        } else if (/^#{1,6}\s/.test(line)) {
            const level = line.match(/^#+/)[0].length;
            out.push(`<h${level}>${inline(line.replace(/^#+\s+/, ''))}</h${level}>`);
            i += 1;
        } else if (line.startsWith('|') && isTableSeparator(lines[i + 1])) {
            const head = splitRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && lines[i].startsWith('|')) {
                rows.push(splitRow(lines[i]));
                i += 1;
            }
            out.push('<table><thead><tr>' + head.map((c) => `<th scope="col">${inline(c)}</th>`).join('')
                + '</tr></thead><tbody>' + rows.map((r) => '<tr>'
                    + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
        } else if (line.startsWith('>')) {
            const quote = [];
            while (i < lines.length && lines[i].startsWith('>')) {
                quote.push(lines[i].replace(/^>\s?/, ''));
                i += 1;
            }
            out.push(`<blockquote>${inline(quote.join(' '))}</blockquote>`);
        } else if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
            const ordered = /^\s*\d+\./.test(line);
            const items = [];
            while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
                items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ''));
                i += 1;
            }
            const tag = ordered ? 'ol' : 'ul';
            out.push(`<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join('')}</${tag}>`);
        } else if (/^---+\s*$/.test(line)) {
            out.push('<hr>');
            i += 1;
        } else if (line.trim() === '') {
            i += 1;
        } else {
            const paragraph = [];
            while (i < lines.length && lines[i].trim() !== '' && !/^(#{1,6}\s|```|>|\||---)/.test(lines[i])
                    && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
                paragraph.push(lines[i]);
                i += 1;
            }
            out.push(`<p>${inline(paragraph.join(' '))}</p>`);
        }
    }
    return out.join('\n');
};

const showReport = async (plugin, file) => {
    const text = await api(`/api/report?plugin=${encodeURIComponent(plugin.path)}&file=${encodeURIComponent(file)}`);
    $('report-body').innerHTML = renderMarkdown(text);
};

const openReports = async (plugin, preferred) => {
    $('report-title').textContent = `Relatórios — ${plugin.component}`;
    const detail = await api(`/api/plugin?path=${encodeURIComponent(plugin.path)}`);
    const select = $('report-select');
    select.replaceChildren(...detail.reports.map((report) => h('option', {value: report.file},
        `${report.date} — ${report.grade || '?'} (${FOCUS_LABELS[report.focus] || report.focus})`
        + (report.partial ? ' parcial' : ''))));
    select.onchange = () => showReport(plugin, select.value);
    $('report-body').replaceChildren();
    $('report-dialog').showModal();
    if (!detail.reports.length) {
        $('report-body').replaceChildren(h('p', {}, 'Nenhum relatório ainda.'));
        return;
    }
    select.value = detail.reports.some((r) => r.file === preferred) ? preferred : detail.reports[0].file;
    await showReport(plugin, select.value);
};

// ---- start --------------------------------------------------------------- //

const fail = (error) => {
    $('queue-status').textContent = `Erro: ${error.message}`;
};

$('check-all').addEventListener('click', () => checkAll().catch(fail));
$('run-form').addEventListener('submit', submitRun);
$('run-form').addEventListener('change', syncRunForm);
$('run-cancel').addEventListener('click', () => $('run-dialog').close());
$('report-close').addEventListener('click', () => $('report-dialog').close());
$('static-close').addEventListener('click', () => $('static-dialog').close());

loadPlugins().catch(fail);
loadRuns().catch(fail);
setInterval(() => loadRuns().catch(fail), POLL_MS);
