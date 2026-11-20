"""签章批次版本纪律测试。

覆盖七条裁定：
1. 同批重算不另起批号，版号递增；
2. 正式/候选分轨，候选不混淆正式结果；
3. 重试同一重算幂等，不重复生成候选；
4. 签章绑定采信/基线/损失同一版证据哈希；
5. 候选切换需证据对齐（快照重放），失败保留旧签章；
6. 陈旧版号 409，切换失败旧版不动；
7. 旧签章版本可原样还原与导出。
"""
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.evidence import evidence_digest, basis as make_basis
import pytest


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'seal.sqlite'))
    return TestClient(main.app)


def _rid(client, dataset='clean'):
    return client.post('/api/runs', json={'dataset': dataset}).json()['manifest']['run_id']


def test_rerun_keeps_batch_id_and_bumps_seq(c):
    rid = _rid(c)
    first = c.get(f'/api/runs/{rid}').json()
    assert first['revision'] == 1
    # 规则版本变化代表现场重算：批号不变。
    rerun = c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    assert rerun.status_code == 200
    second = rerun.json()
    assert second['manifest']['run_id'] == rid
    assert second['revision'] == 2
    assert second['status'] == '候选待审'
    # 正式版仍是 v1，候选没有顶替。
    official = c.get(f'/api/runs/{rid}').json()
    assert official['revision'] == 1
    chain = c.get(f'/api/runs/{rid}/versions').json()
    assert [v['seq'] for v in chain['versions']] == [1, 2]
    assert chain['versions'][0]['role'] == 'official'
    assert chain['versions'][1]['role'] == 'candidate'


def test_retry_same_rerun_is_idempotent(c):
    rid = _rid(c)
    a = c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'}).json()
    b = c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'}).json()
    assert a['revision'] == b['revision'] == 2
    chain = c.get(f'/api/runs/{rid}/versions').json()
    assert len(chain['versions']) == 2
    assert len(chain['candidates']) == 1


def test_new_evidence_supersedes_old_candidate(c):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r4'})
    chain = c.get(f'/api/runs/{rid}/versions').json()['versions']
    assert [v['seq'] for v in chain] == [1, 2, 3]
    assert chain[1]['role'] == 'superseded'
    assert chain[2]['role'] == 'candidate'
    # 同时只有一个正式版。
    current = [v for v in chain if v['role'] in ('official', 'sealed')]
    assert len(current) == 1 and current[0]['seq'] == 1


def test_seal_binds_same_version_evidence(c):
    rid = _rid(c)
    resp = c.post(f'/api/runs/{rid}/seal', json={'revision': 1, 'reviewer': '林复核'})
    assert resp.status_code == 200
    sealed = resp.json()
    assert sealed['status'] == '已签章'
    chain = c.get(f'/api/runs/{rid}/versions').json()['versions'][0]
    assert chain['role'] == 'sealed' and chain['sealed_by'] == '林复核'
    expected = make_basis(evaluate({'dataset': 'clean'}))
    assert chain['seal_basis']['intake_digest'] == expected['intake_digest']
    assert chain['seal_basis']['baseline_digest'] == expected['baseline_digest']
    assert chain['seal_basis']['loss_digest'] == expected['loss_digest']
    assert chain['seal_basis']['evidence_digest'] == chain['evidence_digest']
    # 已签章版本不能再签。
    again = c.post(f'/api/runs/{rid}/seal', json={'revision': 1, 'reviewer': '林复核'})
    assert again.status_code == 409 and again.json()['code'] == 'ALREADY_SEALED'


def test_candidate_cannot_be_sealed_directly(c):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    # 用候选 seq 当 revision 签章：当前正式版是 v1，陈旧冲突。
    resp = c.post(f'/api/runs/{rid}/seal', json={'revision': 2, 'reviewer': '林复核'})
    assert resp.status_code == 409


def test_promote_keeps_old_seal_recoverable(c):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/seal', json={'revision': 1, 'reviewer': '林复核'})
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    ok = c.post(f'/api/runs/{rid}/promote', json={'revision': 1, 'candidate_seq': 2})
    assert ok.status_code == 200
    assert ok.json()['revision'] == 2 and ok.json()['status'] == '待复核'
    chain = c.get(f'/api/runs/{rid}/versions').json()['versions']
    assert chain[0]['role'] == 'superseded' and chain[0]['sealed_by'] == '林复核'
    assert chain[1]['role'] == 'official'
    # 旧签章版本原样还原。
    old = c.get(f'/api/runs/{rid}?seq=1').json()
    assert old['role'] == 'superseded' and old['sealed_by'] == '林复核'
    export = c.get(f'/api/export/{rid}?seq=1')
    assert export.status_code == 200
    import zipfile, io, json
    z = zipfile.ZipFile(io.BytesIO(export.content))
    evidence = json.loads(z.read('evidence.json'))
    assert evidence['sealed_by'] == '林复核'
    assert evidence['seal_basis']['evidence_digest']


def test_stale_promote_conflict_preserves_official(c):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    resp = c.post(f'/api/runs/{rid}/promote', json={'revision': 99, 'candidate_seq': 2})
    assert resp.status_code == 409
    official = c.get(f'/api/runs/{rid}').json()
    assert official['revision'] == 1
    chain = c.get(f'/api/runs/{rid}/versions').json()['versions']
    assert chain[1]['role'] == 'candidate'


def test_misaligned_candidate_rejected(c, tmp_path, monkeypatch):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    # 直接篡改库内候选证据，模拟证据与登记哈希不一致。
    db = main.DB_PATH
    con = store.connect(db)
    row = con.execute('SELECT result FROM run_versions WHERE run_id=? AND seq=2', (rid,)).fetchone()
    import json
    tampered = json.loads(row['result'])
    tampered['balance']['loss_kwh'] = '999.9999'
    con.execute('UPDATE run_versions SET result=? WHERE run_id=? AND seq=2',
                (json.dumps(tampered, ensure_ascii=False), rid))
    con.commit(); con.close()
    resp = c.post(f'/api/runs/{rid}/promote', json={'revision': 1, 'candidate_seq': 2})
    assert resp.status_code == 422 and resp.json()['code'] == 'EVIDENCE_MISALIGNED'
    # 正式版未被顶替。
    official = c.get(f'/api/runs/{rid}').json()
    assert official['revision'] == 1


def test_batch_id_independent_of_rule_version(c):
    r1 = c.post('/api/runs', json={'dataset': 'clean'}).json()
    r2 = c.post('/api/runs', json={'dataset': 'clean', 'rule_version': '2026.11.20-r9'}).json()
    assert r1['manifest']['run_id'] == r2['manifest']['run_id']
    assert r1['manifest']['rule_version'] != r2['manifest']['rule_version']


def test_page_shows_versions_and_candidate_diff(c):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    html = c.get(f'/archive?run_id={rid}').text
    assert '批次版本' in html and '候选差异' in html and 'v1' in html and 'v2' in html
    # 规则版本号变化但证据一致时显示无需顶替；无候选时才提供签章入口。
    assert '无需顶替' in html
    no_candidate = c.post('/api/runs', json={'dataset': 'dirty'}).json()
    page = c.get('/archive?run_id=' + no_candidate['manifest']['run_id']).text
    assert '签章正式版' in page


def test_page_shows_diff_and_actions_when_evidence_diverges(c, tmp_path):
    rid = _rid(c)
    c.post(f'/api/runs/{rid}/rerun', json={'rule_version': '2026.11.20-r3'})
    # 篡改候选结果制造真实证据差异，面板应出现签章/切换操作行。
    import json, sqlite3
    con = sqlite3.connect(main.DB_PATH)
    row = con.execute('SELECT result FROM run_versions WHERE run_id=? AND seq=2', (rid,)).fetchone()
    tampered = json.loads(row[0])
    tampered['balance']['loss_kwh'] = '12.3456'
    con.execute('UPDATE run_versions SET result=? WHERE run_id=? AND seq=2',
                (json.dumps(tampered, ensure_ascii=False), rid))
    con.commit(); con.close()
    html = c.get(f'/archive?run_id={rid}').text
    assert '签章正式版' in html and '切换候选为正式' in html and '有差异' in html


def test_history_migration_from_legacy_db(tmp_path):
    path = str(tmp_path / 'legacy.sqlite')
    result = evaluate({'dataset': 'clean'})
    store.save(path, result)
    # 模拟旧库结构：删除版本表后重新连接应自动迁移。
    import sqlite3
    con = sqlite3.connect(path)
    con.execute('DROP TABLE run_versions')
    con.execute('DROP INDEX IF EXISTS idx_one_official')
    con.commit(); con.close()
    loaded = store.get(path, result['manifest']['run_id'])
    assert loaded['revision'] == 1 and loaded['role'] == 'official'
    assert loaded['evidence_digest'] == evidence_digest(result)
