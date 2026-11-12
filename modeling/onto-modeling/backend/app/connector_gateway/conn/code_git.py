"""代码仓库的加固克隆（ADR-0086；证据采集接口 §10）。

只认 https，主机和端口要在出口白名单里；`init` + `fetch --depth 1 origin <ref>` 取一个提交，不 checkout：
`ls-tree` 列文件，`cat-file --batch` 只读要分析的 blob，符号链接、子模块、超大文件都不落盘。
git 子进程只拿到必要的环境变量（没有代理、没有用户和系统的 git 配置，`HOME` 指任务临时目录）；
令牌经 askpass 读 0600 临时文件，取完就删。git 自己发起的连接不经过子进程里的 Python 审计钩子，
所以这里先核对主机和端口、禁止重定向和 https 以外的协议——这些参数是 git 这一侧唯一的防线。
git 和 worker 同在一个进程组、同受 RLIMIT_AS 限制：单线程解包、限制 mmap 窗口和 malloc arena，省虚拟内存。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .context import ConnError

# 每条 git 命令都带：只放行 https、不跟重定向、钩子指向空、不建符号链接、不递归子模块、不用凭据助手、不起 fsmonitor
HARDEN = (
    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
    "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false", "-c", "submodule.recurse=false",
    "-c", "credential.helper=", "-c", "core.fsmonitor=false",
)
# 省内存：RLIMIT_AS 按虚拟地址算，git 默认 1 GiB 的 mmap 窗口、每个线程的 malloc arena 都会撞上限
LEAN = (
    "-c", "pack.threads=1", "-c", "core.packedGitWindowSize=32m", "-c", "core.packedGitLimit=128m",
    "-c", "core.deltaBaseCacheLimit=32m", "-c", "core.bigFileThreshold=16m",
)
FILE_MAX_BYTES = 1024 * 1024
DEFAULT_USERNAME = "x-access-token"
# 内网仓库的自签 CA：网关只把 ONTO_CONNECTOR_CODE_* 传进子进程，这里转成 git 认的 GIT_SSL_CAINFO
CA_FILE_ENV = "ONTO_CONNECTOR_CODE_CA_FILE"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$|^[0-9a-f:]+$")
_PATH = re.compile(r"^/[A-Za-z0-9._~%+/-]*$")
_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SIZE_POLL_S = 0.2


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str
    explicit_port: bool

    @property
    def url(self) -> str:
        """交给 git、进清单的地址：不带用户信息（用户名经 askpass 给）。"""
        return f"{self.scheme}://{self._netloc()}{self.path}"

    def _netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.explicit_port else host


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    kind: str
    oid: str
    size: int

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    @property
    def is_submodule(self) -> bool:
        return self.mode == "160000" or self.kind == "commit"


def parse_url(raw: str, *, allow_http: bool = False) -> Target:
    """仓库地址：`https://主机[:端口]/路径`，不带用户信息（和网关建任务时的核对一致）。`allow_http` 只给测试放开本机的 http。"""
    raw = str(raw or "").strip()
    if not raw or any(ord(char) < 0x21 or char in "\\\"'`" for char in raw):
        raise ConnError("invalid_connection", "仓库地址不对")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ConnError("invalid_connection", "仓库地址不对") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme != "https" and not (allow_http and scheme == "http" and host in LOCAL_HOSTS):
        raise ConnError("invalid_connection", "仓库地址只支持 https")
    if parts.username is not None or parts.password is not None:
        raise ConnError("invalid_connection", "仓库地址里不能带用户信息，令牌填到访问令牌")
    path = parts.path or "/"
    if not _plain_location(parts, host, path):
        raise ConnError("invalid_connection", "仓库地址不对")
    default = 443 if scheme == "https" else 80
    return Target(scheme, host, port or default, path, port is not None)


def _plain_location(parts, host: str, path: str) -> bool:
    """地址只有主机和路径：没有查询串和片段，主机、路径的写法对，路径里没有 `..` 段。"""
    if parts.query or parts.fragment:
        return False
    return bool(_HOST.match(host)) and bool(_PATH.match(path)) and "/../" not in f"{path}/"


def _valid_ref(ref: str) -> bool:
    if not _REF.match(ref) or ref.startswith(("-", "/")):
        return False
    return ".." not in ref and not ref.endswith((".lock", "/"))


def check_username(value) -> str:
    """askpass 回给 git 的用户名：默认 `x-access-token`；GitLab 的 OAuth 令牌要 `oauth2` 等，由连接参数 `username` 给。"""
    username = str(value or "").strip() or DEFAULT_USERNAME
    if not _USER.match(username):
        raise ConnError("invalid_connection", "用户名的写法不对")
    return username


def check_ref(ref: str) -> str:
    ref = str(ref or "").strip() or "HEAD"
    if not _valid_ref(ref):
        raise ConnError("invalid_connection", "分支或提交的写法不对")
    return ref


def is_commit(ref: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref))


def check_egress(target: Target, egress: list[dict]) -> None:
    for item in egress:
        host = str(item.get("host") or "").strip().lower().strip("[]")
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if host == target.host and port == target.port:
            return
    raise ConnError("egress_denied", f"{target.host}:{target.port} 不在出口白名单里")


ASKPASS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_askpass.sh")


class Workspace:
    """一次克隆的临时目录：`home/`（空的 HOME）、`repo/`（裸取的仓库）、askpass 脚本和令牌文件。"""

    def __init__(self, target: Target, token: str, *, username: str = DEFAULT_USERNAME, allow_http: bool = False):
        self.target = target
        self.username = username
        self.allow_http = allow_http
        self.git = shutil.which("git")
        if not self.git:
            raise ConnError("provider_unavailable", "网关镜像里没有 git")
        self._scratch = tempfile.TemporaryDirectory(prefix="code-repo-", ignore_cleanup_errors=True)
        self.root = self._scratch.name
        self.home = os.path.join(self.root, "home")
        self.repo = os.path.join(self.root, "repo")
        os.mkdir(self.home, 0o700)
        self._token = token
        self._token_file = ""
        self._askpass = ""
        if token:
            self._write_askpass(token)

    def _write_askpass(self, token: str) -> None:
        # git 先问用户名（地址里没有时）再问口令；回答由镜像里固定的 askpass 脚本从文件读，令牌不进命令行和环境变量。
        # 脚本不能写进任务目录：网关的 /tmp 是 noexec 的 tmpfs（11-05 实测 Permission denied）
        self._token_file = os.path.join(self.root, "token")
        fd = os.open(self._token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        self._askpass = ASKPASS

    def drop_token(self) -> None:
        if self._token_file and os.path.exists(self._token_file):
            os.unlink(self._token_file)
        self._token_file = self._askpass = ""

    def close(self) -> None:
        self.drop_token()
        self._scratch.cleanup()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- git ----

    def env(self) -> dict:
        env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "HOME": self.home, "XDG_CONFIG_HOME": self.home,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "LC_ALL": "C", "MALLOC_ARENA_MAX": "2"}
        if self._askpass:
            env.update({"GIT_ASKPASS": self._askpass, "ONTO_GIT_ASKPASS_USER": self.username,
                        "ONTO_GIT_ASKPASS_FILE": self._token_file})
        if os.environ.get(CA_FILE_ENV):
            env["GIT_SSL_CAINFO"] = os.environ[CA_FILE_ENV]
        return env

    def command(self, *args: str) -> list[str]:
        extra = ("-c", "protocol.http.allow=always") if self.allow_http else ()
        return [self.git, *HARDEN, *LEAN, *extra, *args]

    def clean(self, text: str) -> str:
        """git 的报错去掉令牌和地址里的用户信息，只留最后几行。"""
        if self._token:
            text = text.replace(self._token, "***")
        text = re.sub(r"://[^/\s@]+@", "://", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " / ".join(lines[-3:])[:300]

    def run(self, *args: str, timeout: float, cwd: str | None = None, data: bytes | None = None) -> bytes:
        if timeout <= 0:
            raise ConnError("timeout", "超过任务时限")
        try:
            done = subprocess.run(  # NOSONAR(S4721) 固定的 git 参数列表，不经 shell
                self.command(*args), cwd=cwd or self.root, env=self.env(), input=data,
                stdin=None if data is not None else subprocess.DEVNULL, capture_output=True,
                timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise ConnError("timeout", f"git {args[0]} 超过任务时限") from None
        if done.returncode != 0:
            raise _classify(self.clean(done.stderr.decode("utf-8", "replace")), args[0], done.returncode)
        return done.stdout

    def fetch(self, ref: str, *, timeout: float, max_bytes: int) -> None:
        """浅取一个提交；边取边量 `.git` 的大小，超了预算就杀掉。git 留在 worker 的进程组里，运行器杀组时一起杀掉。"""
        stderr_path = os.path.join(self.root, "fetch.err")
        deadline = time.monotonic() + timeout
        with open(stderr_path, "wb") as stderr:
            proc = subprocess.Popen(  # NOSONAR(S4721) 固定的 git 参数列表，不经 shell
                self.command("-C", self.repo, "fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules",
                             "origin", ref),
                cwd=self.root, env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
            failure: ConnError | None = None
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    failure = ConnError("timeout", "git fetch 超过任务时限")
                elif dir_size(self.repo) > max_bytes:
                    failure = ConnError("repo_too_large", "仓库超过采集预算")
                if failure:
                    proc.kill()
                    proc.wait()
                    raise failure
                try:
                    proc.wait(timeout=_SIZE_POLL_S)
                except subprocess.TimeoutExpired:
                    pass
        with open(stderr_path, "rb") as handle:
            message = self.clean(handle.read().decode("utf-8", "replace"))
        if proc.returncode != 0:
            raise _classify(message, "fetch", proc.returncode)
        if dir_size(self.repo) > max_bytes:
            raise ConnError("repo_too_large", "仓库超过采集预算")


# git 报错里的关键字 → 错误码（小写比较）
_AUTH_FAILED = ("authentication failed", "could not read username", "could not read password",
                "terminal prompts disabled", "returned error: 401", "returned error: 403")
_REF_MISSING = ("couldn't find remote ref", "not our ref", "unknown revision", "no such remote ref")
_UNREACHABLE = ("could not resolve host", "failed to connect", "connection refused", "connection timed out",
                "couldn't connect")


def _classify(message: str, step: str, returncode: int = 1) -> ConnError:
    lowered = message.lower()
    if returncode == -signal.SIGXFSZ:
        return ConnError("repo_too_large", "仓库的包文件超过网关的单文件上限")
    if any(key in lowered for key in ("out of memory", "cannot allocate memory", "mmap failed")):
        return ConnError("memory", f"git 超出内存上限：{message}")
    if any(key in lowered for key in _AUTH_FAILED):
        return ConnError("auth_failed", f"仓库拒绝了访问令牌：{message}")
    if any(key in lowered for key in _REF_MISSING):
        return ConnError("ref_not_found", f"找不到这个分支或提交：{message}")
    if any(key in lowered for key in ("returned error: 30", "redirect")):
        return ConnError("redirect_refused", f"仓库地址发生了重定向，不跟随：{message}")
    if any(key in lowered for key in _UNREACHABLE):
        return ConnError("unreachable", f"连不上仓库：{message}")
    return ConnError("git_failed", f"git {step} 失败：{message}")


def dir_size(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class Clone:
    """取下来的一个提交：`commit`、`entries`（全部文件）、`read`（按需读 blob 内容）。"""

    def __init__(self, workspace: Workspace, commit: str, entries: list[Entry], git_version: str = ""):
        self.workspace = workspace
        self.commit = commit
        self.entries = entries
        self.git_version = git_version

    def read(self, entries: list[Entry], *, timeout: float) -> dict[str, bytes]:
        """`cat-file --batch` 一次读完；调用方保证 `entries` 都是普通文件、不超单文件上限、总量在预算内。"""
        if not entries:
            return {}
        data = "".join(f"{entry.oid}\n" for entry in entries).encode("ascii")
        out = self.workspace.run("-C", self.workspace.repo, "cat-file", "--batch", timeout=timeout, data=data)
        contents: dict[str, bytes] = {}
        offset = 0
        for entry in entries:
            end = out.index(b"\n", offset)
            header = out[offset:end].decode("ascii", "replace").split()
            offset = end + 1
            if len(header) != 3 or header[1] != "blob":
                continue
            size = int(header[2])
            contents[entry.path] = out[offset:offset + size]
            offset += size + 1
        return contents

    def close(self) -> None:
        self.workspace.close()


def _parse_tree(out: bytes) -> list[Entry]:
    entries = []
    for item in out.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 4:
            continue
        mode, kind, oid, size = fields
        entries.append(Entry(path.decode("utf-8", "replace"), mode, kind, oid,
                             int(size) if size.isdigit() else 0))
    return entries


def clone(connection: dict, token: str, egress: list[dict], *, remaining_s, max_bytes: int,
          allow_http: bool = False) -> Clone:
    """按 ref 浅取一个提交。`remaining_s` 是取剩余时限的函数（`ctx.remaining_s`）；出错时临时目录已经删掉。"""
    target = parse_url(connection.get("repo_url"), allow_http=allow_http)
    ref = check_ref(connection.get("ref"))
    username = check_username(connection.get("username"))
    check_egress(target, egress)
    workspace = Workspace(target, str(token or "").strip(), username=username, allow_http=allow_http)
    try:
        workspace.run("init", "-q", "--template=", workspace.repo, timeout=remaining_s())
        workspace.run("-C", workspace.repo, "remote", "add", "origin", target.url, timeout=remaining_s())
        workspace.fetch(ref, timeout=remaining_s(), max_bytes=max_bytes)
        workspace.drop_token()
        commit = workspace.run("-C", workspace.repo, "rev-parse", "FETCH_HEAD^{commit}",
                               timeout=remaining_s()).decode("ascii", "replace").strip()
        tree = workspace.run("-C", workspace.repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit,
                             timeout=remaining_s())
        version = workspace.run("--version", timeout=remaining_s()).decode("ascii", "replace").split()
        return Clone(workspace, commit, _parse_tree(tree), version[-1] if version else "")
    except BaseException:
        workspace.close()
        raise


def ls_remote(connection: dict, token: str, egress: list[dict], *, timeout: float,
              allow_http: bool = False) -> list[tuple[str, str]]:
    """`git ls-remote`：验证能访问、令牌能读；回 [(提交号, 引用名)]。"""
    target = parse_url(connection.get("repo_url"), allow_http=allow_http)
    ref = check_ref(connection.get("ref"))
    username = check_username(connection.get("username"))
    check_egress(target, egress)
    with Workspace(target, str(token or "").strip(), username=username, allow_http=allow_http) as workspace:
        out = workspace.run("ls-remote", target.url, *(["HEAD"] if ref == "HEAD" else ["HEAD", ref]),
                            timeout=timeout)
    refs = []
    for line in out.decode("utf-8", "replace").splitlines():
        oid, _, name = line.partition("\t")
        if oid and name:
            refs.append((oid.strip(), name.strip()))
    return refs
