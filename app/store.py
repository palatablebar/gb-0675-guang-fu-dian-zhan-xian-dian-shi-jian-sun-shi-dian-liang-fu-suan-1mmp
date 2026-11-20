"""批次版本存储。

纪律：
- run_id 绑定不可变输入，同一批重算不另起批号，只递增 seq。
- official/sealed/candidate/superseded 四角色，同一时刻最多一个正式版。
- (run_id, evidence_digest) 唯一：重试同一重算命中既有版本，不重复生成候选。
- 签章行永久保留；正式版被顶替后转 superseded，旧签章仍可原样还原。
- 所有角色变更与签章都在单事务内完成，失败回滚，旧版签章不受影响。
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

from app.errors import BusinessError
from app.evidence import evidence_digest, basis as make_basis, seal_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    digest TEXT UNIQUE NOT NULL,
    result TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '待复核'
);
CREATE TABLE IF NOT EXISTS run_versions (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    evidence_digest TEXT NOT NULL,
    role TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result TEXT NOT NULL,
    seal TEXT,
    sealed_at TEXT,
    sealed_by TEXT,
    seal_basis TEXT,
    PRIMARY KEY (run_id, seq),
    UNIQUE (run_id, evidence_digest)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_official
    ON run_versions(run_id)
    WHERE role IN ('official','sealed');
'''


def _now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    _migrate_legacy(con)
    return con


def _migrate_legacy(con):
    """旧库只有 runs 表：把既存批次登记为 seq=1 正式版，历史不丢失。"""
    cols = {r[1] for r in con.execute('PRAGMA table_info(runs)')}
    if not cols:
        return
    for row in con.execute('SELECT id, result, status FROM runs'):
        exists = con.execute(
            'SELECT 1 FROM run_versions WHERE run_id=? AND seq=1', (row['id'],)
        ).fetchone()
        if exists:
            continue
        result = json.loads(row['result'])
        con.execute(
            '''INSERT INTO run_versions
               (run_id,seq,evidence_digest,role,rule_version,created_at,result)
               VALUES(?,?,?,?,?,?,?)''',
            (row['id'], 1, evidence_digest(result), 'official',
             result['manifest']['rule_version'], result['manifest']['generated_at'],
             row['result']),
        )


def _row_to_version(row):
    return {
        'seq': row['seq'],
        'evidence_digest': row['evidence_digest'],
        'role': row['role'],
        'rule_version': row['rule_version'],
        'created_at': row['created_at'],
        'sealed_at': row['sealed_at'],
        'sealed_by': row['sealed_by'],
        'seal': row['seal'],
        'seal_basis': json.loads(row['seal_basis']) if row['seal_basis'] else None,
    }


def _load_result(row):
    result = json.loads(row['result'])
    result.update(revision=row['seq'], status=_status_label(row['role']), role=row['role'],
                  seal=row['seal'], sealed_at=row['sealed_at'], sealed_by=row['sealed_by'],
                  seal_basis=json.loads(row['seal_basis']) if row['seal_basis'] else None,
                  evidence_digest=row['evidence_digest'])
    return result


def _status_label(role):
    return {'official': '待复核', 'sealed': '已签章', 'candidate': '候选待审',
            'superseded': '已被顶替'}.get(role, role)


def _current(con, run_id):
    # 部分唯一索引保证最多一行；不能按 seq 排序，否则更高 seq 的候选会盖过正式版。
    return con.execute(
        'SELECT * FROM run_versions WHERE run_id=? AND role IN (?,?) LIMIT 1',
        (run_id, 'official', 'sealed'),
    ).fetchone()


def save(path, result):
    """登记一次计算结果。

    同证据哈希命中既有版本（正式或候选）直接复用——重试不产生重复候选；
    新证据：候选角色已有别的版本则先作废，再写入下一个 seq。
    """
    run_id = result['manifest']['run_id']
    digest = evidence_digest(result)
    payload = json.dumps(result, ensure_ascii=False)
    with connect(path) as con:
        existing = con.execute(
            'SELECT * FROM run_versions WHERE run_id=? AND evidence_digest=?',
            (run_id, digest),
        ).fetchone()
        if existing:
            return _load_result(existing)
        last = con.execute(
            'SELECT MAX(seq) AS m FROM run_versions WHERE run_id=?', (run_id,)
        ).fetchone()['m']
        seq = (last or 0) + 1
        if seq == 1:
            role = 'official'
        else:
            con.execute(
                "UPDATE run_versions SET role='superseded' WHERE run_id=? AND role='candidate'",
                (run_id,),
            )
            role = 'candidate'
        con.execute(
            '''INSERT INTO run_versions
               (run_id,seq,evidence_digest,role,rule_version,created_at,result)
               VALUES(?,?,?,?,?,?,?)''',
            (run_id, seq, digest, role, result['manifest']['rule_version'],
             result['manifest']['generated_at'], payload),
        )
        # 兼容旧接口：runs 表保留一条批次头，revision 跟随正式版。
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)',
                    (run_id, result['manifest']['sha256'], payload))
        row = con.execute(
            'SELECT * FROM run_versions WHERE run_id=? AND seq=?', (run_id, seq)
        ).fetchone()
        return _load_result(row)


def get(path, run_id, seq=None):
    with connect(path) as con:
        if seq is None:
            row = _current(con, run_id)
        else:
            row = con.execute(
                'SELECT * FROM run_versions WHERE run_id=? AND seq=?', (run_id, seq)
            ).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '批次或版本不存在', 404)
        return _load_result(row)


def versions(path, run_id):
    with connect(path) as con:
        rows = con.execute(
            'SELECT * FROM run_versions WHERE run_id=? ORDER BY seq', (run_id,)
        ).fetchall()
        if not rows:
            raise BusinessError('NOT_FOUND', '批次不存在', 404)
        return [_row_to_version(r) for r in rows]


def candidates(path, run_id):
    with connect(path) as con:
        rows = con.execute(
            "SELECT * FROM run_versions WHERE run_id=? AND role='candidate' ORDER BY seq DESC",
            (run_id,),
        ).fetchall()
        return [_row_to_version(r) for r in rows]


def history(path):
    with connect(path) as con:
        out = []
        for r in con.execute(
            '''SELECT v.run_id, v.seq, v.role, v.rule_version, v.created_at, v.sealed_by, v.sealed_at
               FROM run_versions v
               JOIN (SELECT run_id, MAX(seq) AS m FROM run_versions GROUP BY run_id) x
                 ON x.run_id=v.run_id AND v.seq=x.m
               ORDER BY v.rowid DESC LIMIT 100'''
        ):
            out.append({'id': r['run_id'], 'revision': r['seq'], 'status': _status_label(r['role']),
                        'role': r['role'], 'rule_version': r['rule_version']})
        return out


def _check_revision(con, run_id, revision):
    current = _current(con, run_id)
    if not current:
        raise BusinessError('NOT_FOUND', '批次不存在', 404)
    if current['seq'] != revision:
        raise BusinessError('STALE_VERSION', '版本已更新，请刷新后重试', 409)
    return current


def seal(path, run_id, revision, reviewer):
    """签章：只能签正式版，签章依据固化同一版采信/基线/损失哈希。"""
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise BusinessError('BAD_REVIEWER', '签章人不能为空')
    reviewer = reviewer.strip()
    with connect(path) as con:
        row = _check_revision(con, run_id, revision)
        if row['role'] == 'sealed':
            raise BusinessError('ALREADY_SEALED', '该版本已签章，请勿重复签章', 409)
        result = json.loads(row['result'])
        if evidence_digest(result) != row['evidence_digest']:
            raise BusinessError('EVIDENCE_MISALIGNED', '证据哈希与版本登记不一致，禁止签章')
        at = _now()
        basis_data = make_basis(result)
        seal = seal_hash(row['evidence_digest'], reviewer, at)
        cur = con.execute(
            '''UPDATE run_versions
               SET role='sealed', seal=?, sealed_at=?, sealed_by=?, seal_basis=?
               WHERE run_id=? AND seq=? AND role='official' ''',
            (seal, at, reviewer, json.dumps(basis_data, ensure_ascii=False),
             run_id, revision),
        )
        if cur.rowcount != 1:
            raise BusinessError('STALE_VERSION', '版本已更新，请刷新后重试', 409)
    return get(path, run_id)


def promote(path, run_id, candidate_seq, revision):
    """候选转正式：旧正式版（含签章）转 superseded 保留，单事务原子完成。"""
    with connect(path) as con:
        _check_revision(con, run_id, revision)
        candidate = con.execute(
            "SELECT * FROM run_versions WHERE run_id=? AND seq=? AND role='candidate'",
            (run_id, candidate_seq),
        ).fetchone()
        if not candidate:
            raise BusinessError('CANDIDATE_NOT_FOUND', '候选版本不存在或已处理', 404)
        candidate_result = json.loads(candidate['result'])
        if evidence_digest(candidate_result) != candidate['evidence_digest']:
            raise BusinessError('EVIDENCE_MISALIGNED', '候选证据与登记哈希不一致，禁止顶替正式结果', 422)
        cur = con.execute(
            '''UPDATE run_versions SET role='superseded'
               WHERE run_id=? AND role IN ('official','sealed')''',
            (run_id,),
        )
        if cur.rowcount != 1:
            raise BusinessError('VERSION_CONFLICT', '正式版状态异常，切换已回滚', 409)
        con.execute(
            "UPDATE run_versions SET role='official' WHERE run_id=? AND seq=?",
            (run_id, candidate_seq),
        )
    return get(path, run_id)
