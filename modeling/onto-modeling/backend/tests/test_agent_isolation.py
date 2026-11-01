"""密钥与 agent 身份隔离（方案 E10、第 0 章问题 5、第 6 章第 4、5 项）。

- 两个引擎里 agent 执行的 bash 只有最小环境、没有凭据变量（任何平台都测）；
- 以 agent 身份读不到密钥目录里的文件，工作区照常可写（Linux 上以 root 跑时创建临时系统用户来测，
  Linux 容器全量会跑到；macOS 开发机跳过这一组）。
用户名、Key、令牌全部合成。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from app import config
from app.governance import agent_exec, agent_identity, secrets
from app.services import backup, model_settings, personal_model_settings, service_tokens
from app.services.onto_mcp import tokens as mcp_tokens
from app.services.workspace import WorkspaceManager

SECRET_ENV = {
    "ANTHROPIC_API_KEY": "synthetic-anthropic-key",
    "ANTHROPIC_AUTH_TOKEN": "synthetic-anthropic-token",
    "ONTO_WEB_AUTH_ADMIN_TOKEN": "synthetic-admin-token",
    "ONTO_WEB_MODEL_SECRET_KEY": "synthetic-master-key-0123456789abcdef",
    "ONTO_WEB_JIUWEN_API_KEY": "synthetic-jiuwen-key",
    "SOME_SERVICE_PASSWORD": "synthetic-password",
}
CREDENTIAL_NAMES = set(SECRET_ENV)


def _env_names(output: str) -> set[str]:
    return {line.split("=", 1)[0] for line in output.splitlines() if "=" in line}


# ---- 最小环境 ----


def test_shell_env_drops_backend_config_and_credentials():
    env = {**SECRET_ENV, "PATH": "/usr/bin", "HOME": "/data/home", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "ONTO_WEB_DATA_DIR": "/data/onto-web", "CLAUDE_PROJECT_DIR": "/w", "GIT_EDITOR": "true",
           "HTTPS_PROXY": "http://user:pass@proxy.example.com:8080", "NO_PROXY": "localhost"}
    clean = agent_exec.shell_env(env)
    assert set(clean) == {"PATH", "HOME", "LANG", "LC_ALL", "CLAUDE_PROJECT_DIR", "GIT_EDITOR", "NO_PROXY"}


def test_cli_env_keeps_model_gateway_but_not_backend_secrets():
    env = {**SECRET_ENV, "PATH": "/usr/bin", "ONTO_WEB_DATA_DIR": "/data", "ANTHROPIC_BASE_URL": "https://gw.example.com",
           "IS_SANDBOX": "1", agent_exec.REAL_CLI_ENV: "/opt/cli", agent_exec.AGENT_USER_ENV: "onto-agent"}
    clean = agent_exec.cli_env(env)
    assert {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "IS_SANDBOX", "PATH"} <= set(clean)
    assert not {"ONTO_WEB_AUTH_ADMIN_TOKEN", "ONTO_WEB_MODEL_SECRET_KEY", "ONTO_WEB_JIUWEN_API_KEY",
                "SOME_SERVICE_PASSWORD", "ONTO_WEB_DATA_DIR", agent_exec.REAL_CLI_ENV} & set(clean)


def test_shell_shim_runs_command_with_minimal_env():
    result = subprocess.run(
        [str(agent_identity.SHELL_SHIM), "env"], capture_output=True, text=True, timeout=30,
        env={**os.environ, **SECRET_ENV, "ONTO_AGENT_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stderr
    names = _env_names(result.stdout)
    assert "PATH" in names and not names & CREDENTIAL_NAMES
    assert not any(name.startswith("ONTO_WEB_") for name in names)


def test_claude_cli_wrapper_hands_cli_only_gateway_env_and_shell_prefix(tmp_path):
    """SDK 起 CLI 时并进后端的完整环境；包装之后 CLI 只拿到模型网关配置，bash 前缀指向 shell 包装。"""
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text("#!/bin/sh\nenv\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    options = agent_identity.claude_options({"ANTHROPIC_API_KEY": "synthetic-anthropic-key"})
    assert options["cli_path"] == str(agent_identity.CLI_SHIM)
    env = {**os.environ, **SECRET_ENV, **options["env"], agent_exec.REAL_CLI_ENV: str(fake_cli)}
    result = subprocess.run([options["cli_path"], "--version"], capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    cli_env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert cli_env["ANTHROPIC_API_KEY"] == "synthetic-anthropic-key"  # CLI 自己调模型要用
    assert cli_env["CLAUDE_CODE_SHELL_PREFIX"] == str(agent_identity.SHELL_SHIM)
    assert not {"ONTO_WEB_AUTH_ADMIN_TOKEN", "ONTO_WEB_MODEL_SECRET_KEY", "ONTO_WEB_JIUWEN_API_KEY",
                "SOME_SERVICE_PASSWORD"} & set(cli_env)

    # CLI 里 agent 的 bash 经前缀执行：模型 Key 也不在
    prefixed = subprocess.run([cli_env["CLAUDE_CODE_SHELL_PREFIX"], "env"], capture_output=True, text=True,
                              timeout=30, env=cli_env)
    assert prefixed.returncode == 0, prefixed.stderr
    assert not _env_names(prefixed.stdout) & CREDENTIAL_NAMES


def test_claude_engine_options_use_wrapper(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    from app.services.engines import claude
    from app.services.engines.base import EngineContext

    ctx = EngineContext(
        project_id="p1", cwd=Path(tempfile.gettempdir()), request_permission=lambda *args: None,
        load_resume_token=lambda: None, save_resume_token=lambda token: None, clear_resume_token=lambda: None,
        log_error=lambda *args: None,
    )
    options = claude.ClaudeEngine(ctx)._build_options(None)
    assert str(options.cli_path) == str(agent_identity.CLI_SHIM)
    assert options.env[agent_exec.REAL_CLI_ENV]


def test_jiuwen_commands_run_through_shell_wrapper(monkeypatch):
    pytest.importorskip("openjiuwen")
    from openjiuwen.core.sys_operation.local import shell_operation

    assert agent_identity.install_jiuwen_shell_isolation()
    assert hasattr(shell_operation.ShellOperation._create_subprocess, "__wrapped__")
    assert agent_identity.install_jiuwen_shell_isolation()  # 幂等，不重复包

    async def run() -> str:
        env = agent_identity.jiuwen_env({**os.environ, **SECRET_ENV})
        proc = await asyncio.create_subprocess_shell(
            agent_identity.jiuwen_command("env"), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        return out.decode()

    names = _env_names(asyncio.run(run()))
    assert "PATH" in names and not names & CREDENTIAL_NAMES


# ---- 密钥目录 ----


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SECRETS_DIR", "")
    monkeypatch.setattr(config, "KEY_BACKUP_DIR", "")
    monkeypatch.setattr(config, "BACKUP_DIR", "")
    monkeypatch.delenv("ONTO_WEB_MODEL_SECRET_KEY", raising=False)
    return data


def test_secret_stores_live_in_private_dir(data_dir):
    manager = WorkspaceManager(data_dir, Path(config.ASSETS_DIR))
    model_settings.save("https://gw.example.com", "synthetic-admin-model-key", "synthetic-model")
    personal_model_settings._master_key()
    service_tokens.issue({"name": "合成服务", "scopes": ["releases.read"]})
    with mcp_tokens.database(manager):
        pass
    directory = data_dir / "secrets"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (secrets.MODEL_SETTINGS, secrets.MASTER_KEY, secrets.SERVICE_TOKENS, secrets.MCP_TOKENS):
        path = directory / name
        assert path.is_file(), name
        assert not (data_dir / name).exists()
    for name in (secrets.MODEL_SETTINGS, secrets.MASTER_KEY, secrets.SERVICE_TOKENS):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


def test_legacy_secret_files_are_moved_on_startup(data_dir):
    data_dir.mkdir(parents=True)
    for name in (secrets.MODEL_SETTINGS, secrets.PERSONAL_SETTINGS, secrets.MASTER_KEY, secrets.SERVICE_TOKENS):
        (data_dir / name).write_text("{}" if name.endswith(".json") else "x" * 40, encoding="utf-8")
    (data_dir / secrets.MCP_TOKENS).write_bytes(b"")
    (data_dir / f"{secrets.MCP_TOKENS}-wal").write_bytes(b"")
    moved = secrets.migrate()
    assert set(moved) == {*secrets.SECRET_FILES, f"{secrets.MCP_TOKENS}-wal"}
    assert not any((data_dir / name).exists() for name in secrets.SECRET_FILES)
    assert all(stat.S_IMODE((data_dir / "secrets" / name).stat().st_mode) == 0o600 for name in secrets.SECRET_FILES)


def test_master_key_warning_and_separate_backup(data_dir, tmp_path, monkeypatch):
    warnings = secrets.prepare()
    assert warnings and "ONTO_WEB_MODEL_SECRET_KEY" in warnings[0]
    monkeypatch.setenv("ONTO_WEB_MODEL_SECRET_KEY", SECRET_ENV["ONTO_WEB_MODEL_SECRET_KEY"])
    assert secrets.prepare() == []
    monkeypatch.delenv("ONTO_WEB_MODEL_SECRET_KEY")

    # 自动生成的主密钥：密文进备份，主密钥只进单独的密钥备份目录
    key_dir = tmp_path / "key-backup"
    monkeypatch.setattr(config, "KEY_BACKUP_DIR", str(key_dir))
    personal_model_settings._master_key()
    manager = WorkspaceManager(data_dir, Path(config.ASSETS_DIR))
    model_settings.save("", "synthetic-admin-model-key", "")
    name = backup.run_backup(manager)
    with tarfile.open(backup.backups_dir() / name) as tar:
        names = tar.getnames()
    assert "data/secrets/model-settings.json" in names
    assert not any(".model-secret-key" in item for item in names)
    copied = key_dir / "model-secret-key"
    assert copied.read_bytes() == (data_dir / "secrets" / secrets.MASTER_KEY).read_bytes()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600


# ---- 以 agent 身份读不到密钥（Linux + root） ----

AGENT_TEST_USER = "onto-agent-test"
linux_root = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0 or not shutil.which("useradd"),
    reason="切换用户要在 Linux 上以 root 运行（Linux 容器全量会跑到）",
)


@pytest.fixture()
def agent_env(monkeypatch):
    import pwd

    try:
        pwd.getpwnam(AGENT_TEST_USER)
    except KeyError:
        subprocess.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", AGENT_TEST_USER],
                       check=True)
    # 临时目录的上级要让 agent 能穿过（pytest 的 tmp 目录归 root 0700）
    base = Path(tempfile.mkdtemp(prefix="onto-agent-", dir="/tmp"))
    base.chmod(0o755)
    data, home = base / "data", base / "home"
    gitconfig = base / "agent-gitconfig"
    gitconfig.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    gitconfig.chmod(0o644)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SECRETS_DIR", "")
    monkeypatch.setattr(config, "AGENT_USER", AGENT_TEST_USER)
    monkeypatch.setenv("HOME", str(home))
    previous_umask = os.umask(0o022)
    try:
        yield data, home
    finally:
        os.umask(previous_umask)
        shutil.rmtree(base, ignore_errors=True)


def _as_agent(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # 镜像里用 `git config --system safe.directory '*'` 放过「仓库归 root」的检查；这里指一份 agent 读得到的
    # 全局配置（GIT_CONFIG_GLOBAL）模拟
    git_safe = {"GIT_CONFIG_GLOBAL": str(Path(config.DATA_DIR).parent / "agent-gitconfig")}
    env = {**os.environ, **SECRET_ENV, **agent_identity.jiuwen_env(os.environ), **git_safe}
    return subprocess.run([str(agent_identity.SHELL_SHIM), command], capture_output=True, text=True, timeout=30,
                          env=env, cwd=cwd)


@linux_root
def test_agent_cannot_read_secrets_but_can_write_workspace(agent_env, monkeypatch):
    data, home = agent_env
    manager = WorkspaceManager(data, Path(config.ASSETS_DIR))
    meta = manager.create_project("隔离演示", owner="operator_a")
    model_settings.save("https://gw.example.com", "synthetic-admin-model-key", "synthetic-model")
    personal_model_settings._master_key()
    service_tokens.issue({"name": "合成服务", "scopes": ["releases.read"]})
    with mcp_tokens.database(manager):
        pass
    assembled = data / "assembled" / "h1" / "commands"  # 插件运行时的装配目录：agent 只读
    assembled.mkdir(parents=True)
    (assembled / "ontology.md").write_text("synthetic command\n", encoding="utf-8")
    sessions = data / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "other.jsonl").write_text("synthetic conversation\n", encoding="utf-8")

    result = agent_identity.prepare_filesystem()
    assert result["switching"] and result["user"] == AGENT_TEST_USER

    import pwd

    uid = pwd.getpwnam(AGENT_TEST_USER).pw_uid
    assert _as_agent("id -u").stdout.strip() == str(uid)
    assert "0" not in _as_agent("id -G").stdout.split()  # 不带 root 组
    secrets_root = data / "secrets"
    for name in (secrets.MODEL_SETTINGS, secrets.MASTER_KEY, secrets.SERVICE_TOKENS, secrets.MCP_TOKENS):
        denied = _as_agent(f"cat '{secrets_root / name}'")
        assert denied.returncode != 0 and "synthetic-admin-model-key" not in denied.stdout, name
    assert _as_agent(f"ls '{secrets_root}'").returncode != 0
    assert _as_agent(f"cat '{sessions / 'other.jsonl'}'").returncode != 0  # 数据目录其余部分也读不到
    command = assembled / "ontology.md"
    assert _as_agent(f"cat '{command}'").stdout.strip() == "synthetic command"
    assert _as_agent(f"echo x >> '{command}'").returncode != 0
    assert _as_agent(f"ls '{data}'").returncode != 0

    root = manager.project_root(meta["id"])
    written = _as_agent("echo agent-edit >> research/agent-note.md && git status --porcelain", cwd=root)
    assert written.returncode == 0, written.stderr
    assert (root / "research" / "agent-note.md").read_text(encoding="utf-8").strip() == "agent-edit"
    # 后端随后的保存照常提交，agent 之后也还能接着改后端写过的文件
    manager.write_file(meta["id"], "research/web-note.md", "web\n", author="operator_a")
    assert _as_agent("echo more >> research/web-note.md", cwd=root).returncode == 0
    assert _as_agent(f"touch '{home}/agent-home-ok'").returncode == 0
