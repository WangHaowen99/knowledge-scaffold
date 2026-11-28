"""Process-local synthetic credentials for deployment tests."""

from __future__ import annotations

import secrets
from pathlib import Path

AUTH_ADMIN_TOKEN = secrets.token_urlsafe(32)
BOOTSTRAP_ADMIN_SECRET = f"Aa1!{secrets.token_urlsafe(18)}"
MODELING_SERVICE_TOKEN = secrets.token_hex(32)


def prepare_consumer_integration(deploy_dir):
    deploy_dir = Path(deploy_dir)
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / 'integration_config.sh').write_text('''#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/env_config.sh"
if [ "${1:-}" = --check-running ]; then
    python3 "$(dirname "$0")/integration_probe.py"
    exit $?
fi
python3 - "$@" <<'PYTHON'
import pathlib
import secrets
import sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
consumer = pathlib.Path(args['--consumer-env'])
hub = pathlib.Path(args['--hub-env'])
def read(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if '=' in line and not line.startswith('#')) if path.exists() else {}
values, remote = read(consumer), read(hub)
assert args['--issuer']
assert args['--hub-url'].startswith(('http://', 'https://'))
assert args['--evaluation-url'].startswith(('http://', 'https://'))
assert values.get('FLYWHEEL_SERVICE_TOKEN')
token = values.get('HUB_CONSUMER_SERVICE_TOKEN') or remote.get('HUB_CONSUMER_SERVICE_TOKEN') or secrets.token_hex(32)
assert not remote.get('HUB_CONSUMER_SERVICE_TOKEN') or remote['HUB_CONSUMER_SERVICE_TOKEN'] == token
values.update(HUB_CONSUMER_SERVICE_TOKEN=token, HUB_AUTH_ISSUER=args['--issuer'],
              HUB_BASE_URL=args['--hub-url'], FLYWHEEL_EVALUATION_URL=args['--evaluation-url'])
values.setdefault('HUB_ENABLED', 'true')
values.setdefault('HUB_INSTANCE_ID', 'synthetic-hub-instance')
remote['HUB_CONSUMER_SERVICE_TOKEN'] = token
for path, config in ((consumer, values), (hub, remote)):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(f'{key}={value}\\n' for key, value in config.items()))
    path.chmod(0o600)
PYTHON
''')
    (deploy_dir / 'env_config.sh').write_text('# Synthetic consumer dependency.\n')
    (deploy_dir / 'integration_probe.py').write_text(
        'import os\nraise SystemExit(int(os.getenv("STUB_CONNECTION_EXIT", "0")))\n')
