/* ═══════════════════════════════════════════════════════════
   PBI Generator — Main Application JS
   ═══════════════════════════════════════════════════════════ */

let currentPbiData = null;
let isEditing = false;

function checkAuth(res) {
    if (res.redirected || res.url.includes('/login')) {
        window.location.href = '/login';
        return false;
    }
    return true;
}

// ── Epic loading ──────────────────────────────────────────────

async function loadEpics() {
    const sel = document.getElementById('epicSelect');
    sel.innerHTML = '<option value="">-- Loading epics... --</option>';
    try {
        const res = await fetch('/api/epics');
        if (!checkAuth(res)) return;
        if (!res.ok) {
            sel.innerHTML = '<option value="">-- Configure Azure DevOps in Settings --</option>';
            return;
        }
        const epics = await res.json();
        sel.innerHTML = '<option value="">-- Select an Epic --</option>';
        epics.forEach(e => {
            const opt = document.createElement('option');
            opt.value = e.title;
            opt.textContent = `${e.title} (#${e.id})`;
            sel.appendChild(opt);
        });
        const saved = localStorage.getItem('pbi_last_epic');
        if (saved) {
            const match = Array.from(sel.options).find(o => o.value === saved);
            if (match) sel.value = saved;
        }
    } catch (e) {
        sel.innerHTML = '<option value="">-- Failed to load epics --</option>';
    }
}

// ── PBI Generation ────────────────────────────────────────────

document.getElementById('pbiForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    await generatePbi();
});

document.getElementById('regenerateBtn')?.addEventListener('click', async () => {
    await generatePbi();
});

async function generatePbi() {
    const btn = document.getElementById('generateBtn');
    const reqText = document.getElementById('pbiRequest').value.trim();
    const epicTitle = document.getElementById('epicSelect').value;

    if (!reqText) return;

    btn.disabled = true;
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('resultCard').style.display = 'none';

    const steps = [
        'Analyzing request...',
        'Fetching features...',
        'Generating PBI with AI...',
        'Parsing response...',
    ];
    let stepIdx = 0;
    btn.innerHTML = `<span class="pbi-spinner"></span> ${steps[0]}`;
    const stepTimer = setInterval(() => {
        stepIdx++;
        if (stepIdx < steps.length) {
            btn.innerHTML = `<span class="pbi-spinner"></span> ${steps[stepIdx]}`;
        }
    }, 2500);

    try {
        const res = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ request: reqText, epic_title: epicTitle }),
        });
        clearInterval(stepTimer);
        if (!checkAuth(res)) return;
        const data = await res.json();

        if (!res.ok) {
            showResult('danger', data.error || 'Generation failed.');
            return;
        }

        currentPbiData = data;
        renderPreview(data);
        document.getElementById('previewCard').style.display = 'block';
    } catch (e) {
        clearInterval(stepTimer);
        showResult('danger', 'Network error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-stars"></i> Generate PBI';
    }
}

// ── Preview rendering ─────────────────────────────────────────

function renderPreview(data) {
    isEditing = false;
    const body = document.getElementById('previewBody');

    const acList = (data.acceptance_criteria || [])
        .map(c => `<li>${escHtml(c)}</li>`).join('');

    const tags = (data.tags || [])
        .map(t => `<span class="pbi-badge pbi-badge--muted">${escHtml(t)}</span>`).join('');

    const priorityClass = data.priority <= 1 ? 'pbi-badge--amber' : data.priority === 2 ? 'pbi-badge--blue' : 'pbi-badge--muted';

    body.innerHTML = `
        <div id="previewContent">
            <h3 class="pbi-preview__title">${escHtml(data.title)}</h3>
            <p class="pbi-preview__desc">${escHtml(data.description)}</p>
            <h4 class="pbi-preview__section-label">Acceptance Criteria</h4>
            <ul class="pbi-preview__criteria">${acList}</ul>
            <div class="pbi-preview__inline-fields">
                <div class="pbi-preview__inline-field">
                    <label class="pbi-label">Priority</label>
                    <select class="pbi-select" id="inlinePriority">
                        <option value="1" ${data.priority === 1 ? 'selected' : ''}>1 - High</option>
                        <option value="2" ${data.priority === 2 ? 'selected' : ''}>2 - Medium</option>
                        <option value="3" ${data.priority === 3 ? 'selected' : ''}>3 - Low</option>
                    </select>
                </div>
                <div class="pbi-preview__inline-field">
                    <label class="pbi-label">Effort</label>
                    <select class="pbi-select" id="inlineEffort">
                        ${[1,2,3,5,8,13].map(v => `<option value="${v}" ${data.effort === v ? 'selected' : ''}>${v} pts</option>`).join('')}
                    </select>
                </div>
                <div class="pbi-preview__inline-field">
                    <label class="pbi-label">Parent Feature</label>
                    <input type="number" class="pbi-input" id="inlineParentFeature" placeholder="Feature ID" value="${data.parent_feature_id || ''}">
                    ${data.parent_feature_name ? `<div class="pbi-hint">${escHtml(data.parent_feature_name)}</div>` : ''}
                </div>
            </div>
            <div class="pbi-preview__meta" style="margin-top:0.75rem;">
                ${tags}
            </div>
        </div>
        <div id="editContent" style="display:none;">
            <div class="pbi-field">
                <label class="pbi-label">Title</label>
                <input type="text" class="pbi-input" id="editTitle" value="${escAttr(data.title)}">
            </div>
            <div class="pbi-field">
                <label class="pbi-label">Description</label>
                <textarea class="pbi-textarea" id="editDesc" rows="3">${escHtml(data.description)}</textarea>
            </div>
            <div class="pbi-field">
                <label class="pbi-label">Acceptance Criteria (one per line)</label>
                <textarea class="pbi-textarea" id="editAc" rows="4">${(data.acceptance_criteria || []).join('\n')}</textarea>
            </div>
            <div class="pbi-field">
                <label class="pbi-label">Tags (comma-separated)</label>
                <input type="text" class="pbi-input" id="editTags" value="${(data.tags || []).join(', ')}">
            </div>
            <button class="pbi-btn pbi-btn--primary pbi-btn--sm" id="applyEditBtn">Apply Changes</button>
        </div>
    `;

    // Inline field change handlers
    document.getElementById('inlinePriority').onchange = (e) => {
        currentPbiData.priority = parseInt(e.target.value) || 2;
    };
    document.getElementById('inlineEffort').onchange = (e) => {
        currentPbiData.effort = parseInt(e.target.value) || 3;
    };
    document.getElementById('inlineParentFeature').onchange = (e) => {
        const val = e.target.value.trim();
        currentPbiData.parent_feature_id = val ? parseInt(val) || null : null;
    };

    document.getElementById('editToggle').onclick = () => {
        isEditing = !isEditing;
        document.getElementById('previewContent').style.display = isEditing ? 'none' : 'block';
        document.getElementById('editContent').style.display = isEditing ? 'block' : 'none';
    };

    const applyBtn = document.getElementById('applyEditBtn');
    if (applyBtn) {
        applyBtn.onclick = () => {
            currentPbiData.title = document.getElementById('editTitle').value;
            currentPbiData.description = document.getElementById('editDesc').value;
            currentPbiData.acceptance_criteria = document.getElementById('editAc').value.split('\n').filter(l => l.trim());
            currentPbiData.tags = document.getElementById('editTags').value.split(',').map(t => t.trim()).filter(Boolean);
            renderPreview(currentPbiData);
        };
    }
}

// ── Create in Azure DevOps ────────────────────────────────────

document.getElementById('createBtn')?.addEventListener('click', async () => {
    if (!currentPbiData) return;

    const btn = document.getElementById('createBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="pbi-spinner"></span> Creating...';

    const sprintTarget = document.getElementById('sprintTarget').value;
    const duplicateAction = document.getElementById('duplicateAction').value;

    try {
        const res = await fetch('/api/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pbi_data: currentPbiData,
                next_sprint: sprintTarget === 'next',
                backlog: sprintTarget === 'backlog',
                update_existing: duplicateAction === 'update',
            }),
        });
        if (!checkAuth(res)) return;
        const data = await res.json();

        if (!res.ok) {
            showResult('danger', data.error || 'Creation failed.');
            return;
        }

        const urlLink = data.url
            ? `<a href="${data.url}" target="_blank" class="pbi-btn pbi-btn--ghost pbi-result__link"><i class="bi bi-box-arrow-up-right"></i> Open in Azure DevOps</a>`
            : '';
        showResult('success', `
            <div class="pbi-result">
                <div class="pbi-result__icon pbi-result__icon--success"><i class="bi bi-check-circle-fill"></i></div>
                <h4 class="pbi-result__title">PBI #${data.id} ${data.action}!</h4>
                <p class="pbi-result__sub">${data.iteration || 'Backlog'}</p>
                ${urlLink}
            </div>
        `);
    } catch (e) {
        showResult('danger', 'Network error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-upload"></i> Create in Azure DevOps';
    }
});

// ── Helpers ───────────────────────────────────────────────────

function showResult(type, html) {
    const card = document.getElementById('resultCard');
    const body = document.getElementById('resultBody');
    card.style.display = 'block';
    if (type === 'danger') {
        body.innerHTML = `<div class="pbi-alert pbi-alert--danger"><i class="bi bi-exclamation-triangle"></i> ${html}</div>`;
    } else {
        body.innerHTML = html;
    }
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

function escAttr(s) {
    return (s || '').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

document.getElementById('refreshEpics')?.addEventListener('click', loadEpics);
document.getElementById('epicSelect')?.addEventListener('change', (e) => {
    if (e.target.value) {
        localStorage.setItem('pbi_last_epic', e.target.value);
    }
});

// ── Init ──────────────────────────────────────────────────────

loadEpics();
