"""测试共用：内存里的知识包 Hub 替身（发布走 Hub，ADR-0098），以及跨项目测试导入同级 knowledge-hub 源码。"""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import threading
import uuid

import pytest

# 跨项目的 Hub 测试导入同级 knowledge-hub 源码，没有时跳过
for parent in Path(__file__).resolve().parents:
    hub_backend = parent / 'knowledge-hub' / 'backend'
    if hub_backend.exists():
        sys.path.append(str(hub_backend))
        break

# 用内存 Hub 的测试模块：发布、发布记录与门禁相关的测试
SYNTHETIC_HUB_MODULES = (
    'test_modeling_eval', 'test_publish_binding_gate', 'test_eval_import', 'test_eval_presets',
    'test_release_publish', 'test_contracts', 'test_solver_contract_check', 'test_solver_package_roundtrip',
    'test_ownership', 'test_i1_ui_copy', 'test_i1_followups', 'test_api_compat', 'test_publish_render_regression',
    'test_f_platform_release',
)

HUB_CLAIMS = {'sub': 'operator_a', 'iss': 'https://example.com'}
HUB_TOKEN = 'synthetic-access'
HUB_SUBJECT = '["https://example.com", "operator_a"]'


class SyntheticHub:
    """只实现建模侧用到的发布、查询、下载协议；版本号按包递增 0.1.N，评测快照 <基础版本>-eval.N。"""

    def __init__(self, root: Path):
        self.root = root
        self.requests: dict[str, dict] = {}
        self.authorized: dict[str, dict] = {}  # 包 → {sub: 授权用户}（共享授权同步用，develop !590）
        self.calls: list[tuple[str, str]] = []
        self.unavailable = False
        self.lock = threading.Lock()

    def _request(self, request_id):
        from app.services.workspace import WorkspaceError

        row = self.requests.get(request_id)
        if row is None:
            raise WorkspaceError('Synthetic Hub request not found', 404, 'not_found')
        return row

    def _version(self, body):
        rows = [r for r in self.requests.values() if r['ontology_id'] == body['ontology_id']]
        if body.get('kind') == 'evaluation_snapshot':
            base = next((r['version'] for r in rows if r['release_id'] == body.get('base_release_id')), '0.1.0')
            return f"{base}-eval.{sum(1 for r in rows if r['version'].startswith(base + '-eval.')) + 1}"
        return f"0.1.{sum(1 for r in rows if r.get('kind', 'release') == 'release') + 1}"

    def committed(self, ontology_id):
        return [r for r in self.requests.values() if r['ontology_id'] == ontology_id and r['status'] == 'committed']

    def call(self, method, path, token='', body=None, upload=None, headers=None, download=None, upload_name=None,
             **_options):
        from app.services import knowledge_hub
        from app.services.workspace import WorkspaceError

        if self.unavailable:
            raise WorkspaceError('知识包 Hub 不可用，请稍后重试', 503, 'hub_unavailable')
        parts = path.split('?')[0].strip('/').split('/')
        with self.lock:
            self.calls.append((method, path))
            if path == '/api/package-publish-requests':
                if body['request_id'] not in self.requests:
                    self.requests[body['request_id']] = dict(
                        body, status='pending', release_id=uuid.uuid4().hex, version=self._version(body),
                        name=f"synthetic-{body['ontology_id'][:8]}")
                return dict(self.requests[body['request_id']])
            if method == 'GET' and len(parts) == 3 and parts[:2] == ['api', 'package-publish-requests']:
                return dict(self._request(parts[-1]))
            if parts[-1] == 'content':
                row = self._request(parts[-2])
                shutil.copyfile(upload, self.root / row['release_id'])
                if knowledge_hub.file_digest(self.root / row['release_id']) != headers['X-Package-Digest']:
                    raise AssertionError('Synthetic Hub upload digest mismatch')
                row.update(status='uploaded', package_digest=headers['X-Package-Digest'])
                return dict(row)
            if parts[-1] == 'commit':
                row = self._request(parts[-2])
                row.update(status='committed', committed_at=datetime.now(timezone.utc).isoformat())
                return dict(row)
            if parts[:2] == ['api', 'packages'] and parts[3:4] == ['authorized-users']:
                granted = self.authorized.setdefault(parts[2], {})
                if method == 'PUT':
                    for user in body['users']:
                        granted[user['sub']] = {**user, 'subject_id': user['sub']}
                if method == 'DELETE':
                    granted.pop(parts[-1], None)
                return {'items': list(granted.values()), 'total': len(granted), 'next_offset': None}
            if parts[:2] == ['api', 'packages'] and parts[3:4] == ['releases']:
                if len(parts) == 4:
                    rows = sorted(self.committed(parts[2]), key=lambda r: r['committed_at'], reverse=True)
                    return {'items': [dict(r) for r in rows], 'total': len(rows), 'offset': 0, 'limit': 20,
                            'next_offset': None}
                row = next((r for r in self.committed(parts[2]) if r['release_id'] == parts[4]), None)
                if row is None:
                    raise WorkspaceError('Hub 包不存在或当前用户无权访问', 404, 'not_found')
                if download:
                    shutil.copyfile(self.root / row['release_id'], download)
                    return Path(download)
                return dict(row)
        raise AssertionError(f'Unexpected synthetic Hub operation: {method} {path}')


# 自己测 /userinfo 核对的模块：不替换 auth.current_user
LIVE_USERINFO_MODULES = ('test_first_login_gate',)


@pytest.fixture(autouse=True)
def auth_userinfo_stub(request, monkeypatch):
    """鉴权中间件每次都向 Auth 的 /userinfo 核对账号（ADR-0107）；测试里没有 Auth，默认按令牌身份放行、不待改密。"""
    if request.module.__name__.split('.')[-1] in LIVE_USERINFO_MODULES:
        return
    from app.services import auth

    def current_user(token, claims):
        role = claims.get('role') if claims.get('role') in ('admin', 'user', 'ops') else 'user'
        return {'sub': claims.get('sub'), 'role': role, 'password_change_required': False}

    monkeypatch.setattr(auth, 'current_user', current_user)


@pytest.fixture
def synthetic_hub(tmp_path, monkeypatch):
    from app.services import hub_packages

    archives = tmp_path / 'synthetic-hub'
    archives.mkdir(exist_ok=True)
    hub = SyntheticHub(archives)
    monkeypatch.setattr(hub_packages, 'call', hub.call)
    return hub


@pytest.fixture(autouse=True)
def evaluation_hub_archive(request, monkeypatch):
    """发布相关的测试模块默认接上内存 Hub。"""
    if request.module.__name__.split('.')[-1] not in SYNTHETIC_HUB_MODULES:
        return None
    return request.getfixturevalue('synthetic_hub')


@pytest.fixture
def hub_login(monkeypatch):
    """开启鉴权、认合成登录令牌；返回带令牌的请求头。"""
    from app.services import auth

    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: dict(HUB_CLAIMS) if token == HUB_TOKEN else None)
    return {'Authorization': f'Bearer {HUB_TOKEN}'}
