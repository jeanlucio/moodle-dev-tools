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

// Report dates arrive as YYYY-MM-DD; show them the Brazilian way.
const formatReportDate = (date) => date.split('-').reverse().join('/');

const formatDuration = (seconds) => {
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    return minutes ? `${minutes}min ${total % 60}s` : `${total}s`;
};

// ---- plugins ------------------------------------------------------------- //

// One report as chips, for the group rows, where each row is a single run.
const reportCell = (audit) => {
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
    return h('div', {}, chips, h('small', {}, formatReportDate(audit.date)));
};

const auditCell = (audit, grades) => {
    if (!audit) {
        return h('span', {class: 'hint'}, 'nunca');
    }
    const {quality, security} = grades;
    const gradeChips = (report, label) => {
        const chips = [chip(`${label} ${report.grade}`, gradeTone(report.grade))];
        if (report.partial) {
            chips.push(chip('parcial'));
        }
        return chips;
    };
    // A focused run grades only one side; the other side keeps the last run that graded it, with its own date.
    const lines = [];
    if (quality) {
        lines.push(h('div', {}, gradeChips(quality, 'Qual'), h('small', {}, formatReportDate(quality.date))));
    }
    if (security) {
        lines.push(h('div', {}, gradeChips(security, 'Seg'), h('small', {}, formatReportDate(security.date))));
    }
    return h('div', {}, lines);
};

const groupsLine = (summary) => (summary
    ? h('small', {}, `Grupos: ${summary.covered}/${summary.total} auditados`
        + (summary.closed ? ` · ${summary.closed} encerrado(s)` : ''))
    : null);

const staticCell = (plugin) => {
    const summary = state.staticSummary[plugin.path];
    if (summary === undefined) {
        return h('span', {class: 'hint'}, '—');
    }
    if (summary.error) {
        return h('span', {class: 'chip bad', title: summary.error}, 'erro');
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
        h('td', {}, auditCell(plugin.last_audit, plugin.last_grades), groupsLine(plugin.groups_summary)),
        h('td', {}, staticCell(plugin)),
        h('td', {},
            h('button', {type: 'button', class: 'btn', onclick: () => openRunDialog(plugin)}, 'Auditar'),
            h('button', {type: 'button', class: 'btn', onclick: () => showStatic(plugin)}, 'Checar'),
            plugin.groups_summary
                ? h('button', {type: 'button', class: 'btn', onclick: () => openGroups(plugin)}, 'Grupos') : null,
            h('button', {type: 'button', class: 'btn', onclick: () => openReports(plugin)}, 'Relatórios')),
    )));
};

const loadPlugins = async () => {
    state.plugins = await api('/api/plugins');
    renderPlugins();
};

const summarize = (results) => Object.fromEntries(
    Object.entries(results).filter(([, list]) => list.length).map(([name, list]) => [name, list.length]));

/** One plugin at a time, so the button counts up and a failing plugin does not hide the rest. */
const checkAll = async () => {
    const button = $('check-all');
    button.disabled = true;
    let failed = 0;
    for (const [index, plugin] of state.plugins.entries()) {
        button.textContent = `Checando ${index + 1}/${state.plugins.length}…`;
        try {
            const {results} = await api(`/api/static?plugin=${encodeURIComponent(plugin.path)}`);
            state.staticSummary[plugin.path] = summarize(results);
        } catch (error) {
            failed += 1;
            state.staticSummary[plugin.path] = {error: error.message};
        }
        renderPlugins();
    }
    button.disabled = false;
    button.textContent = 'Checar todos (determinístico)';
    const dirty = Object.values(state.staticSummary).filter((s) => !s.error && Object.keys(s).length).length;
    const clean = state.plugins.length - dirty - failed;
    $('queue-status').textContent = `Checagens: ${clean} limpo(s), ${dirty} com ocorrência(s)`
        + (failed ? `, ${failed} com erro` : '');
    if (failed) {
        showBanner(`${failed} plugin(s) não puderam ser checados. Passe o mouse no chip "erro" ou use Checar no plugin.`);
    }
};

const showStatic = async (plugin) => {
    $('static-title').textContent = `Checagens — ${plugin.component}`;
    const body = $('static-body');
    body.replaceChildren(h('p', {class: 'hint'}, 'Rodando…'));
    $('static-dialog').showModal();
    try {
        const {results} = await api(`/api/static?plugin=${encodeURIComponent(plugin.path)}`);
        state.staticSummary[plugin.path] = summarize(results);
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
    const focus = form.elements.focus.value;
    return {
        plugin: state.current.path,
        focus,
        only: [...document.querySelectorAll('#groups input:checked')].map((box) => box.value),
        since: scope === 'since' ? $('since').value : null,
        // The quality focus reads only what changed since the last tag unless told to read it all.
        full: focus === 'quality' && scope === 'default',
        with_tests: $('with-tests').checked,
        no_verify: $('no-verify').checked,
        no_phpstan: $('no-phpstan').checked && focus !== 'security',
        phpstan_level: Number($('phpstan-level').value),
    };
};

const syncRunForm = () => {
    const focus = $('run-form').elements.focus.value;
    // The security focus never runs PHPStan, so there is nothing to skip.
    $('no-phpstan').disabled = focus === 'security';
    $('phpstan-level').disabled = focus === 'security' || $('no-phpstan').checked;
    $('no-phpstan-text').textContent = focus === 'security'
        ? 'PHPStan: a rodada de segurança já não o usa'
        : 'Pular o PHPStan (mais rápido, perde os bugs de tipo e de null que ele acha)';
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

const openRunDialog = async (plugin, preset = {}) => {
    state.current = plugin;
    $('run-title').textContent = `Auditar ${plugin.component}`;
    $('run-error').hidden = true;
    $('run-form').reset();
    if (preset.focus) {
        $('run-form').elements.focus.value = preset.focus;
    }
    $('estimate').textContent = 'Calculando estimativa…';
    $('run-dialog').showModal();
    const detail = await api(`/api/plugin?path=${encodeURIComponent(plugin.path)}`);
    const groups = $('groups');
    groups.replaceChildren(...(detail.groups.length
        ? detail.groups.map((group) => h('label', {class: 'check'},
            h('input', {type: 'checkbox', value: group.name, onchange: syncRunForm,
                checked: preset.group === group.name}),
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

const LOG_TAIL_CHARS = 60000;
const BOTTOM_SLACK_PX = 24;

/** "criada ... · duração ..." for a run; the duration of a run in progress keeps counting. */
const metaText = (run) => {
    const meta = [`criada ${formatDate(run.created)}`];
    if (run.started) {
        const end = run.finished || Date.now() / 1000;
        meta.push(`duração ${formatDuration(end - run.started)}`);
    }
    return meta.join(' · ');
};

const runCard = async (run) => {
    const active = ['queued', 'running'].includes(run.status);
    const actions = [];
    if (active) {
        actions.push(h('button', {type: 'button', class: 'btn', onclick: () => cancelRun(run.id)}, 'Cancelar'));
    }
    if (run.status === 'inconclusive' && !run.resumed_by) {
        actions.push(h('button', {type: 'button', class: 'btn primary',
            onclick: () => resumeRun(run.id)}, 'Continuar'));
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
        h('pre', {}, logText.slice(-LOG_TAIL_CHARS) || '(vazia)'));
    return h('div', {class: 'run'},
        h('div', {class: 'run-head'}, h('strong', {}, runTitle(run)),
            chip(STATUS_LABELS[run.status] || run.status, STATUS_TONE[run.status])),
        h('div', {class: 'meta', 'data-started': run.started, 'data-finished': run.finished},
            metaText(run)),
        progress ? h('div', {class: 'progress'}, progress) : null,
        run.status === 'inconclusive'
            ? h('div', {class: 'meta'}, run.resumed_by
                ? 'A cota acabou; esta execução já foi retomada em outra.'
                : 'A cota acabou: o que foi lido ficou em cache. Quando ela renovar, Continuar paga só o que faltou.')
            : null,
        logText || active ? details : null,
        actions.length ? h('div', {class: 'row-actions'}, actions) : null);
};

/** Where the log box of each run is scrolled, so a rebuild of the cards does not throw it back up. */
const captureScroll = () => {
    const saved = new Map();
    document.querySelectorAll('#runs .run').forEach((node) => {
        const box = node.querySelector('details[open] pre');
        saved.set(node.dataset.id, {
            open: node.querySelector('details[open]') !== null,
            top: box ? box.scrollTop : 0,
            // Following the end of the log is the default; scrolling up to read stops it.
            follow: box ? box.scrollHeight - box.scrollTop - box.clientHeight <= BOTTOM_SLACK_PX : true,
        });
    });
    return saved;
};

const restoreScroll = (saved) => {
    document.querySelectorAll('#runs .run').forEach((node) => {
        const before = saved.get(node.dataset.id);
        const box = node.querySelector('details[open] pre');
        if (box) {
            box.scrollTop = !before || before.follow ? box.scrollHeight : before.top;
        }
    });
};

let loadingRuns = false;

const loadRuns = async () => {
    // A slow answer must not pile polls up behind it.
    if (loadingRuns) {
        return;
    }
    loadingRuns = true;
    try {
        await refreshRuns();
    } finally {
        loadingRuns = false;
    }
};

const refreshRuns = async () => {
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
    const cards = await Promise.all(runs.map(runCard));
    // Read the scroll only now, after the requests: the user may have moved while they ran.
    const saved = captureScroll();
    cards.forEach((card, index) => {
        card.dataset.id = runs[index].id;
        const details = card.querySelector('details');
        if (details && saved.get(runs[index].id)?.open) {
            details.open = true;
        }
    });
    $('runs').replaceChildren(...cards);
    restoreScroll(saved);
};

/** Keeps the duration of running runs ticking every second, independent of the polling. */
const tickDurations = () => {
    document.querySelectorAll('#runs .meta[data-started]').forEach((node) => {
        const started = Number(node.dataset.started);
        if (!started || node.dataset.finished) {
            return;
        }
        node.textContent = node.textContent.replace(/duração .*$/, `duração ${formatDuration(Date.now() / 1000 - started)}`);
    });
};

const cancelRun = async (id) => {
    await api(`/api/runs/${id}/cancel`, {method: 'POST', body: {}});
    await loadRuns();
};

const resumeRun = async (id) => {
    try {
        await api(`/api/runs/${id}/resume`, {method: 'POST', body: {}});
    } catch (failure) {
        window.alert(failure.message);
    }
    await loadRuns();
};

// ---- groups -------------------------------------------------------------- //

const groupRow = (plugin, group) => {
    const last = group.last;
    const lastCell = last
        ? h('div', {}, reportCell(last), last.scope === 'plugin' ? chip('plugin inteiro') : null)
        : h('span', {class: 'hint'}, 'nunca auditado');
    const ignored = group.ignored
        ? h('small', {class: 'ignored'}, `Relatório de ${formatReportDate(group.ignored.date)} não conta: ${group.ignored.reason}.`)
        : null;
    return h('tr', {},
        h('td', {}, h('strong', {}, group.name), group.tests ? h('small', {}, 'lê tests/') : null),
        h('td', {}, `${group.files} arquivos`, h('small', {}, `${group.lines} linhas`)),
        h('td', {}, lastCell, ignored),
        h('td', {}, group.closed ? chip('encerrada', 'good') : chip('aberta')),
        h('td', {}, h('button', {type: 'button', class: 'btn', onclick: () => {
            $('groups-dialog').close();
            openRunDialog(plugin, {focus: 'security', group: group.name});
        }}, 'Auditar')));
};

const openGroups = async (plugin) => {
    $('groups-title').textContent = `Grupos — ${plugin.component}`;
    const body = document.querySelector('#groups-table tbody');
    body.replaceChildren();
    $('groups-dialog').showModal();
    const detail = await api(`/api/plugin?path=${encodeURIComponent(plugin.path)}`);
    body.replaceChildren(...detail.groups.map((group) => groupRow(plugin, group)));
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
        `${formatReportDate(report.date)} — ${report.grade || '?'} (${FOCUS_LABELS[report.focus] || report.focus})`
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

const showBanner = (message) => {
    $('banner-text').textContent = message;
    $('banner').hidden = false;
};

const fail = (error) => {
    const hint = /host não permitido/.test(error.message)
        ? ' Abra a página por localhost ou 127.0.0.1 (não pelo nome da máquina).' : '';
    showBanner(`${error.message}.${hint}`);
};

$('check-all').addEventListener('click', () => checkAll().catch(fail));
$('banner-close').addEventListener('click', () => { $('banner').hidden = true; });
$('run-form').addEventListener('submit', submitRun);
$('run-form').addEventListener('change', syncRunForm);
$('run-cancel').addEventListener('click', () => $('run-dialog').close());
$('report-close').addEventListener('click', () => $('report-dialog').close());
$('static-close').addEventListener('click', () => $('static-dialog').close());
$('groups-close').addEventListener('click', () => $('groups-dialog').close());

loadPlugins().catch(fail);
loadRuns().catch(fail);
setInterval(() => loadRuns().catch(fail), POLL_MS);
setInterval(tickDurations, 1000);
