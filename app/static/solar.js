const solar = { busy: false };
const status = document.querySelector('#feedback');
const runId = document.body.dataset.runId;

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.detail || data.code || '操作失败');
  return data;
}

document.querySelector('#recompute').addEventListener('click', async () => {
  if (solar.busy) return;
  solar.busy = true;
  status.textContent = '同批复算中…';
  try {
    const dataset = document.querySelector('#dataset').value;
    // 始终走同批 rerun：批号不变，新证据生成候选，重试命中既有版本不重复建候选。
    const run = await postJSON('/api/runs/' + runId + '/rerun', { dataset });
    status.textContent = '已生成第 ' + run.revision + ' 版（' + run.status + '）';
    const url = new URL(location.href);
    url.searchParams.set('run_id', run.manifest.run_id);
    url.searchParams.delete('seq');
    location.href = url;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    solar.busy = false;
  }
});

const sealBtn = document.querySelector('#seal-btn');
if (sealBtn) {
  sealBtn.addEventListener('click', async () => {
    if (solar.busy) return;
    const reviewer = (document.querySelector('#reviewer') || {}).value || '';
    if (!reviewer.trim()) { status.textContent = '请填写签章人'; return; }
    solar.busy = true;
    status.textContent = '签章核验中…';
    try {
      await postJSON('/api/runs/' + runId + '/seal', {
        revision: Number(document.body.dataset.revision),
        reviewer: reviewer.trim(),
      });
      status.textContent = '签章完成，证据已固化';
      location.reload();
    } catch (error) {
      status.textContent = '签章未生效：' + error.message;
    } finally {
      solar.busy = false;
    }
  });
}

const promoteBtn = document.querySelector('#promote-btn');
if (promoteBtn) {
  promoteBtn.addEventListener('click', async () => {
    if (solar.busy) return;
    solar.busy = true;
    status.textContent = '正在重放候选证据并核验…';
    try {
      const seq = promoteBtn.dataset.seq;
      await postJSON('/api/runs/' + runId + '/promote', {
        revision: Number(document.body.dataset.revision),
        candidate_seq: Number(seq),
      });
      status.textContent = '候选已切换为正式，旧版签章保留可还原';
      const url = new URL(location.href);
      url.searchParams.delete('seq');
      location.href = url;
    } catch (error) {
      status.textContent = '切换失败，旧版签章保持不变：' + error.message;
    } finally {
      solar.busy = false;
    }
  });
  promoteBtn.dataset.seq = promoteBtn.dataset.seq ||
    (document.querySelectorAll('.version-chip.role-candidate b')[0]?.textContent.replace('v', '') || '');
}

const filePicker = document.querySelector('#csv-file');
if (filePicker) {
  const editor = document.querySelector('#csv-text');
  filePicker.addEventListener('change', async () => {
    const file = filePicker.files[0];
    if (!file) return;
    if (file.size > 2000000) { editor.value = ''; alert('文件超过2MB，请缩小后导入'); return; }
    const bytes = await file.arrayBuffer();
    try { editor.value = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch (e) { editor.value = new TextDecoder('gb18030', { fatal: true }).decode(bytes); }
  });
  document.querySelector('#import-csv').addEventListener('click', async event => {
    event.target.disabled = true;
    try {
      const response = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csv_text: editor.value }),
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail);
      location.href = location.pathname + '?run_id=' + data.manifest.run_id;
    } catch (e) {
      editor.setCustomValidity(e.message);
      editor.reportValidity();
    } finally {
      event.target.disabled = false;
    }
  });
  editor.addEventListener('input', () => editor.setCustomValidity(''));
}
