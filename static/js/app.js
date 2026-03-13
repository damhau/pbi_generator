/* PBI Generator - Main Application JS */

let currentPbiData = null;
let isEditing = false;

// ── Epic loading ──────────────────────────────────────────────

async function loadEpics() {
    const sel = document.getElementById('epicSelect');
    sel.innerHTML = '<option value="">-- Loading epics... --</option>';
    try {
        const res = await fetch('/api/epics');
        if (!res.ok) {
            const err = await res.json();
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
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Generating...';
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('resultCard').style.display = 'none';

    try {
        const res = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ request: reqText, epic_title: epicTitle }),
        });
        const data = await res.json();

        if (!res.ok) {
            showResult('danger', data.error || 'Generation failed.');
            return;
        }

        currentPbiData = data;
        renderPreview(data);
        document.getElementById('previewCard').style.display = 'block';
    } catch (e) {
        showResult('danger', 'Network error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-stars me-1"></i>Generate PBI';
    }
}

// ── Preview rendering ─────────────────────────────────────────

function renderPreview(data) {
    isEditing = false;
    const body = document.getElementById('previewBody');

    const acList = (data.acceptance_criteria || []).map(c => `<li>${escHtml(c)}</li>`).join('');
    const tags = (data.tags || []).map(t => `<span class="badge bg-secondary me-1">${escHtml(t)}</span>`).join('');

    body.innerHTML = `
        <div id="previewContent">
            <h5 class="mb-3">${escHtml(data.title)}</h5>
            <p class="text-body-secondary">${escHtml(data.description)}</p>
            <h6>Acceptance Criteria</h6>
            <ul class="mb-3">${acList}</ul>
            <div class="row g-3 mb-3">
                <div class="col-auto">
                    <span class="badge bg-primary fs-6">Priority: ${data.priority}</span>
                </div>
                <div class="col-auto">
                    <span class="badge bg-info fs-6">Effort: ${data.effort} pts</span>
                </div>
                <div class="col-auto">${tags}</div>
            </div>
            ${data.parent_feature_id ? `<p class="mb-0"><i class="bi bi-diagram-3 me-1"></i>Parent Feature: <strong>#${data.parent_feature_id}</strong> ${escHtml(data.parent_feature_name || '')}</p>` : '<p class="mb-0 text-body-secondary"><i class="bi bi-diagram-3 me-1"></i>No parent feature selected</p>'}
        </div>
        <div id="editContent" style="display:none;">
            <div class="mb-2">
                <label class="form-label fw-semibold">Title</label>
                <input type="text" class="form-control" id="editTitle" value="${escAttr(data.title)}">
            </div>
            <div class="mb-2">
                <label class="form-label fw-semibold">Description</label>
                <textarea class="form-control" id="editDesc" rows="3">${escHtml(data.description)}</textarea>
            </div>
            <div class="mb-2">
                <label class="form-label fw-semibold">Acceptance Criteria (one per line)</label>
                <textarea class="form-control" id="editAc" rows="4">${(data.acceptance_criteria || []).join('\n')}</textarea>
            </div>
            <div class="row g-2 mb-2">
                <div class="col">
                    <label class="form-label fw-semibold">Priority (1-3)</label>
                    <input type="number" class="form-control" id="editPriority" min="1" max="3" value="${data.priority}">
                </div>
                <div class="col">
                    <label class="form-label fw-semibold">Effort (1-13)</label>
                    <input type="number" class="form-control" id="editEffort" min="1" max="13" value="${data.effort}">
                </div>
            </div>
            <div class="mb-2">
                <label class="form-label fw-semibold">Tags (comma-separated)</label>
                <input type="text" class="form-control" id="editTags" value="${(data.tags || []).join(', ')}">
            </div>
            <button class="btn btn-sm btn-primary" id="applyEditBtn">Apply Changes</button>
        </div>
    `;

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
            currentPbiData.priority = parseInt(document.getElementById('editPriority').value) || 2;
            currentPbiData.effort = parseInt(document.getElementById('editEffort').value) || 3;
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
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Creating...';

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
        const data = await res.json();

        if (!res.ok) {
            showResult('danger', data.error || 'Creation failed.');
            return;
        }

        const urlLink = data.url ? `<a href="${data.url}" target="_blank" class="btn btn-outline-primary mt-2"><i class="bi bi-box-arrow-up-right me-1"></i>Open in Azure DevOps</a>` : '';
        showResult('success', `
            <i class="bi bi-check-circle display-4 text-success"></i>
            <h4 class="mt-2">PBI #${data.id} ${data.action}!</h4>
            <p class="text-body-secondary">${data.iteration || 'Backlog'}</p>
            ${urlLink}
        `);
    } catch (e) {
        showResult('danger', 'Network error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-upload me-1"></i>Create in Azure DevOps';
    }
});

// ── Helpers ───────────────────────────────────────────────────

function showResult(type, html) {
    const card = document.getElementById('resultCard');
    const body = document.getElementById('resultBody');
    card.style.display = 'block';
    if (type === 'danger') {
        body.innerHTML = `<div class="alert alert-danger mb-0"><i class="bi bi-exclamation-triangle me-1"></i>${html}</div>`;
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

// ── Init ──────────────────────────────────────────────────────

loadEpics();
