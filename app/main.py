from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store
from app.archive import serialize
from app.evidence import evidence_digest


def _verify_replay(run_id, candidate):
    """用候选自带的不可变输入快照重放引擎，证据必须与登记哈希逐位对齐。"""
    if 'input_snapshot' not in candidate:
        raise BusinessError('INPUT_SNAPSHOT_MISSING', '候选缺少源快照，无法核验')
    replayed = evaluate(
        {'dataset': candidate['source'],
         'rule_version': candidate['manifest']['rule_version']},
        snapshot=candidate['input_snapshot'],
    )
    if replayed['manifest']['run_id'] != run_id:
        raise BusinessError('EVIDENCE_MISALIGNED', '重放输入与批次不一致，候选禁止顶替正式结果', 422)
    if evidence_digest(replayed) != candidate['manifest'].get('evidence_digest',
                                                              evidence_digest(candidate)):
        raise BusinessError('EVIDENCE_MISALIGNED', '重放证据与候选登记不一致，候选禁止顶替正式结果', 422)


@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH, evaluate())
    yield


app = FastAPI(title=META['title'], lifespan=lifespan)
app.mount('/static', StaticFiles(directory=str(ROOT / 'app/static')), name='static')


@app.exception_handler(BusinessError)
async def business_error(request: Request, error: BusinessError):
    return JSONResponse({'code': error.code, 'detail': error.message}, status_code=error.status)


@app.get('/')
def home():
    return RedirectResponse('/' + MODULES[0], status_code=303)


@app.get('/healthz')
def health():
    return {'status': 'ok', 'project': META['short']}


@app.post('/api/runs')
def create(payload: dict = Body(default={})):
    return store.save(DB_PATH, evaluate(payload))


@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id: str, payload: dict = Body(default={})):
    previous = store.get(DB_PATH, run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING', '旧批缺少源快照，请重新导入')
    result = evaluate(
        {'dataset': previous['source'], 'rule_version': payload.get('rule_version')},
        snapshot=previous['input_snapshot'],
    )
    if result['manifest']['run_id'] != run_id:
        raise BusinessError('EVIDENCE_MISALIGNED', '重放批号与原批不一致，已拒绝')
    return store.save(DB_PATH, result)


@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)


@app.get('/api/runs/{run_id}/versions')
def run_versions(run_id: str):
    return {'run_id': run_id, 'versions': store.versions(DB_PATH, run_id),
            'candidates': store.candidates(DB_PATH, run_id)}


@app.get('/api/runs/{run_id}')
def run(run_id: str, seq: int | None = None):
    return store.get(DB_PATH, run_id, seq)


@app.post('/api/runs/{run_id}/seal')
def seal(run_id: str, payload: dict = Body(...)):
    revision = payload.get('revision')
    reviewer = payload.get('reviewer')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    return store.seal(DB_PATH, run_id, revision, reviewer)


@app.post('/api/runs/{run_id}/promote')
def promote(run_id: str, payload: dict = Body(...)):
    revision = payload.get('revision')
    candidate_seq = payload.get('candidate_seq')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    if not isinstance(candidate_seq, int) or candidate_seq < 1:
        raise BusinessError('BAD_CANDIDATE', '候选版号须为正整数')
    candidate = store.get(DB_PATH, run_id, candidate_seq)
    _verify_replay(run_id, candidate)
    return store.promote(DB_PATH, run_id, candidate_seq, revision)


@app.get('/api/export/{run_id}')
def export(run_id: str, seq: int | None = None):
    result = store.get(DB_PATH, run_id, seq)
    filename = run_id if seq is None else f'{run_id}-v{seq}'
    return Response(serialize(result), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{filename}.zip"'})


for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
