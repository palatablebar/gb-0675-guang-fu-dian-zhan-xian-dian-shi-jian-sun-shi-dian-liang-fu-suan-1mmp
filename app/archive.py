import io
import json
import zipfile

from app.errors import BusinessError

ROLE_LABEL = {'official': '正式待复核', 'sealed': '正式已签章',
              'candidate': '候选待审', 'superseded': '历史版本（已被顶替）'}


def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN', '批次尚未完成')
    role = result.get('role', 'official')
    manifest = {
        **result['manifest'],
        'revision': result.get('revision', 1),
        'status': result.get('status', '待复核'),
        'role': role,
        'role_label': ROLE_LABEL.get(role, role),
    }
    evidence = {
        'counts': result['counts'],
        'views': result['views'],
        'quarantine': result['quarantine'],
        'seal': result.get('seal'),
        'sealed_at': result.get('sealed_at'),
        'sealed_by': result.get('sealed_by'),
        'seal_basis': result.get('seal_basis'),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('evidence.json', json.dumps(evidence, ensure_ascii=False, indent=2))
    return buffer.getvalue()
