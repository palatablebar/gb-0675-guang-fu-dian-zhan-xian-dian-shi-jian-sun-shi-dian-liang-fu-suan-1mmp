import hashlib, json
from datetime import datetime, timezone


def digest(raw, context):
    """批号只绑定不可变输入（源字节+上下文），与规则版本脱钩。

    规则升级后重算仍是同一批号，规则版本作为版本证据另行固化。
    """
    return hashlib.sha256(
        raw + json.dumps(context, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def make_manifest(source_digest, source, elapsed, rule_version):
    return {'run_id': source_digest[:16], 'sha256': source_digest, 'rule_version': rule_version,
            'source': source, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'elapsed_ms': round(elapsed * 1000, 3), 'timezone': 'Asia/Shanghai'}
