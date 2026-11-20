"""证据哈希、签章依据与候选差异。

批号只绑定不可变输入（源字节+上下文），规则版本属于版本证据的一部分。
每个版本的证据由三个业务域组成：采信、基线、损失；签章时三域哈希一同固化，
保证一个签章永远指向同一版证据，候选与正式结果无法互相顶替。
"""
import hashlib
import json


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _hash(parts):
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part if isinstance(part, bytes) else _canon(part))
        digest.update(b'|')
    return digest.hexdigest()


def intake_digest(result):
    """采信域：采纳记录、隔离记录与行守恒计数。"""
    return _hash([result.get('records', []), result.get('quarantine', []), result.get('counts', {})])


def baseline_digest(result):
    """基线域：每片段对照逆变器、基线功率与原因码。"""
    return _hash([result.get('estimates', [])])


def loss_digest(result):
    """损失域：逐片段损失电量与能量对账结论。"""
    return _hash([result.get('energy_rows', []), result.get('balance', {})])


def evidence_digest(result):
    """版本总证据哈希：规则版本 + 输入哈希 + 三业务域。"""
    return _hash([
        result['manifest']['rule_version'],
        result['manifest']['sha256'],
        intake_digest(result),
        baseline_digest(result),
        loss_digest(result),
    ])


def basis(result):
    """签章依据：签章绑定的同一版采信、基线、损失证据及关键指标。"""
    balance = result.get('balance', {})
    return {
        'rule_version': result['manifest']['rule_version'],
        'input_digest': result['manifest']['sha256'],
        'intake_digest': intake_digest(result),
        'baseline_digest': baseline_digest(result),
        'loss_digest': loss_digest(result),
        'evidence_digest': evidence_digest(result),
        'loss_kwh': balance.get('loss_kwh'),
        'segments': len(result.get('energy_rows', [])),
        'unknown_segments': balance.get('unknown_segments'),
        'quarantined': len(result.get('quarantine', [])),
    }


def seal_hash(evidence_hash, reviewer, sealed_at):
    return hashlib.sha256(_canon({
        'evidence_digest': evidence_hash,
        'reviewer': reviewer,
        'sealed_at': sealed_at,
    })).hexdigest()


def _row_key(row):
    return row.get('device'), row.get('start')


def diff(official, candidate):
    """正式版与候选版的结构化差异：三域一致性 + 关键指标 + 片段级变更数。"""
    domains = []
    for name, fn in (('采信', intake_digest), ('基线', baseline_digest), ('损失', loss_digest)):
        old, new = fn(official), fn(candidate)
        domains.append({'name': name, 'official': old, 'candidate': new, 'aligned': old == new})

    metrics = []
    ob, cb = official.get('balance', {}), candidate.get('balance', {})
    metric_defs = [
        ('确定损失 kWh', 'loss_kwh', lambda r: r.get('balance', {}).get('loss_kwh')),
        ('未定片段', 'unknown_segments', lambda r: r.get('balance', {}).get('unknown_segments')),
        ('并集时长 s', 'union_seconds', lambda r: r.get('balance', {}).get('union_seconds')),
        ('隔离行数', 'quarantined', lambda r: len(r.get('quarantine', []))),
        ('核算片段数', 'segments', lambda r: len(r.get('energy_rows', []))),
    ]
    for label, key, getter in metric_defs:
        old, new = getter(official), getter(candidate)
        metrics.append({'name': label, 'key': key, 'official': old, 'candidate': new, 'changed': old != new})

    old_rows = {_row_key(r): r for r in official.get('energy_rows', [])}
    new_rows = {_row_key(r): r for r in candidate.get('energy_rows', [])}
    changed_segments = sum(
        1 for key, row in new_rows.items()
        if key not in old_rows or any(
            old_rows[key].get(col) != row.get(col)
            for col in ('baseline_kw', 'actual_kw', 'loss_kwh', 'code', 'reference_ids')
        )
    ) + sum(1 for key in old_rows if key not in new_rows)

    return {
        'identical': all(d['aligned'] for d in domains) and not any(m['changed'] for m in metrics),
        'domains': domains,
        'metrics': metrics,
        'changed_segments': changed_segments,
    }
