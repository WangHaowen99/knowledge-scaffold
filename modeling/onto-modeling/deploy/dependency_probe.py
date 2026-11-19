#!/usr/bin/env python3
"""Probe the declared Auth and Consumer endpoints without changing either owner."""

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

PROBE = r'''
import json,os,sys,urllib.error,urllib.request
auth=os.environ['PROBE_AUTH_URL'].rstrip('/')
consumer=os.environ['PROBE_CONSUMER_URL'].rstrip('/')
issuer=os.environ['PROBE_AUTH_ISSUER']
pending=[]
def fetch(url):
    try:
        with urllib.request.urlopen(url,timeout=5) as response:
            return response.status,response.read(65536)
    except urllib.error.HTTPError as error:
        return error.code,None
    except (urllib.error.URLError,TimeoutError):
        return None,None
status,_=fetch(auth+'/health')
if status is None or status==503:
    pending.append('auth')
elif status!=200:
    sys.exit(2)
else:
    status,body=fetch(auth+'/.well-known/openid-configuration')
    if status is None: pending.append('auth')
    elif status!=200 or json.loads(body).get('issuer')!=issuer: sys.exit(2)
    else:
        status,body=fetch(auth+'/jwks.json')
        if status is None: pending.append('auth')
        elif status!=200 or not any(k.get('kty')=='RSA' and k.get('alg')=='RS256'
                                     for k in json.loads(body).get('keys',[])): sys.exit(2)
status,_=fetch(consumer+'/ready')
if status is None or status==503: pending.append('consumer')
elif status!=200: sys.exit(2)
print(json.dumps(sorted(set(pending))))
'''


def probe(image, network, auth_url, issuer, consumer_url):
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker executable is unavailable")
    command = [docker, "run", "--rm", "--pull", "never", "--network", network,
               "-e", "PROBE_AUTH_URL=" + auth_url,
               "-e", "PROBE_AUTH_ISSUER=" + issuer,
               "-e", "PROBE_CONSUMER_URL=" + consumer_url,
               "--entrypoint", "python", image, "-c", PROBE]
    result = subprocess.run(command, text=True, capture_output=True, timeout=45,
                            shell=False)  # NOSONAR(S4721): fixed docker argv
    if result.returncode:
        raise RuntimeError("Auth or Consumer endpoint has an invalid response; inspect the configured dependencies")
    pending = json.loads(result.stdout)
    if not isinstance(pending, list) or any(item not in ("auth", "consumer") for item in pending):
        raise RuntimeError("dependency probe returned an invalid result")
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--check-only", action="store_true",
                        help="check an existing deployment without replacing its dependency state")
    parser.add_argument("--require-ready", action="store_true",
                        help="keep an existing healthy deployment strict during upgrade")
    parser.add_argument("--image", required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--auth-url", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--consumer-url", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--container", default="onto-modeling")
    args = parser.parse_args()
    try:
        if args.check_only and args.verify:
            raise RuntimeError("--check-only and --verify cannot be combined")
        if args.state_file.is_file():
            previous = json.loads(args.state_file.read_text())
            for key, expected in (("authUrl", args.auth_url), ("authIssuer", args.issuer),
                                  ("authAudience", args.audience),
                                  ("consumerUrl", args.consumer_url), ("network", args.network)):
                if previous.get(key) != expected:
                    raise RuntimeError("dependency contract changed; review the existing deployment first")
            if previous.get("phase") != "ready" and previous.get("image") != args.image:
                raise RuntimeError("finish dependency verification before changing the Modeling image")
        pending = probe(args.image, args.network, args.auth_url, args.issuer, args.consumer_url)
        if args.require_ready and pending:
            raise RuntimeError("existing Modeling installation requires Auth and Consumer to be ready before upgrade")
        if args.check_only:
            return 0
        if args.verify and not pending:
            docker = shutil.which("docker")
            if docker is None:
                raise RuntimeError("Docker executable is unavailable")
            for container, port, path in ((args.container, 8100, "/api/health"),
                                          ("knowledge-hub", 8200, "/api/health"),
                                          ("knowledge-evaluation", 5180, "/evaluation/api/health")):
                status = subprocess.run([docker, "exec", container, "python", "-c",  # NOSONAR(S4721): fixed docker argv
                                         "import urllib.request; "
                                         f"r=urllib.request.urlopen('http://127.0.0.1:{port}{path}',timeout=5); "
                                         "raise SystemExit(0 if r.status==200 else 1)"],
                                        text=True, capture_output=True, timeout=10, shell=False)
                if status.returncode:
                    raise RuntimeError(f"{container} HTTP service is not ready yet")
        state = {"schemaVersion": 1, "phase": ("waiting-dependencies" if pending else
                 "ready" if args.verify else "verifying-service"),
                 "pendingDependencies": pending,
                 "authUrl": args.auth_url, "authIssuer": args.issuer,
                 "authAudience": args.audience,
                 "consumerUrl": args.consumer_url, "network": args.network, "image": args.image}
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=args.state_file.parent,
                                         prefix=".dependencies-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(args.state_file)
        logger.info("Modeling dependencies: %s%s", state["phase"],
                    " (" + ", ".join(pending) + ")" if pending else "")
        return 1 if args.verify and pending else 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.error("Modeling dependency check failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
