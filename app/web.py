from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store
from app.evidence import diff as evidence_diff

templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def render(request: Request, key):
    from app.main import DB_PATH
    run_id = request.query_params.get('run_id')
    seq_raw = request.query_params.get('seq')
    seq = int(seq_raw) if seq_raw and seq_raw.isdigit() else None
    if run_id:
        state = store.get(DB_PATH, run_id, seq)
        version_chain = store.versions(DB_PATH, run_id)
        candidate_rows = store.candidates(DB_PATH, run_id)
        viewing = next((v for v in version_chain if v['seq'] == state['revision']), None)
        candidate = (store.get(DB_PATH, run_id, candidate_rows[0]['seq'])
                     if candidate_rows and viewing and viewing['role'] in ('official', 'sealed')
                     else None)
        comparison = evidence_diff(store.get(DB_PATH, run_id), candidate) if candidate else None
    else:
        state = store.save(DB_PATH, evaluate(
            {'dataset': request.query_params.get('dataset', 'dirty')}))
        version_chain = store.versions(DB_PATH, state['manifest']['run_id'])
        candidate_rows, comparison, viewing = [], None, version_chain[0]
    return templates.TemplateResponse(
        request=request, name=f'{key}.html',
        context={'meta': META, 'key': key, 'state': state, 'view': state['views'][key],
                 'version_chain': version_chain, 'candidate_rows': candidate_rows,
                 'viewing': viewing, 'comparison': comparison})
