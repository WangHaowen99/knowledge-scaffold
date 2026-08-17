"""Generate JiuwenBox security policies for onto-agent script sandboxes."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .paths import posix_path_string


_BLOCKED_SYSCALLS = [
    "kexec_load",
    "kexec_file_load",
    "reboot",
    "pivot_root",
    "swapon",
    "swapoff",
    "nfsservctl",
    "lookup_dcookie",
    "ptrace",
    "mount",
    "umount2",
    "unshare",
    "setns",
    "userfaultfd",
    "perf_event_open",
    "bpf",
    "add_key",
    "request_key",
    "keyctl",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "open_by_handle_at",
    "name_to_handle_at",
    "init_module",
    "finit_module",
    "delete_module",
    "acct",
]

_SYSTEM_BIND_MOUNTS = [
    {"host_path": "/bin", "sandbox_path": "/bin", "mode": "ro"},
    {"host_path": "/sbin", "sandbox_path": "/sbin", "mode": "ro"},
    {"host_path": "/usr", "sandbox_path": "/usr", "mode": "ro"},
    {"host_path": "/usr/local", "sandbox_path": "/usr/local", "mode": "ro"},
    {"host_path": "/lib", "sandbox_path": "/lib", "mode": "ro"},
    {"host_path": "/lib64", "sandbox_path": "/lib64", "mode": "ro"},
    {"host_path": "/etc/resolv.conf", "sandbox_path": "/etc/resolv.conf", "mode": "ro"},
    {"host_path": "/etc/hosts", "sandbox_path": "/etc/hosts", "mode": "ro"},
    {"host_path": "/etc/nsswitch.conf", "sandbox_path": "/etc/nsswitch.conf", "mode": "ro"},
    {"host_path": "/etc/ssl/certs", "sandbox_path": "/etc/ssl/certs", "mode": "ro"},
    {"host_path": "/etc/pki", "sandbox_path": "/etc/pki", "mode": "ro"},
]

_BLOCKED_METADATA_IPS = ["169.254.169.254/32"]


@dataclass(frozen=True)
class PolicyContext:
    run_id: str
    workspace_id: str | None
    ontology_id: str | None
    workspace_root: Path
    sandbox_runtime_python_dir: Path
    data_proxy_url: str | None
    memory_max: str
    cpu_max: float
    pids_max: int
    writable_paths: tuple[Path, ...] = ()
    read_only_paths: tuple[Path, ...] = ()


def _normalize_service_url(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    if "://" not in normalized:
        return f"https://{normalized}"
    return normalized


def extract_host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(_normalize_service_url(url))
    host = (parsed.hostname or "").strip().lower()
    return host or None


def extract_port_from_url(url: str | None) -> int | None:
    if not url:
        return None
    parsed = urlparse(_normalize_service_url(url))
    if parsed.port is not None:
        return int(parsed.port)
    scheme = (parsed.scheme or "https").lower()
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    if scheme in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}:
        return 7687
    return None


def build_egress_allowed_ports(data_proxy_url: str | None) -> list[int]:
    """Return egress ports for the run-scoped data proxy only."""
    host = extract_host_from_url(data_proxy_url)
    if not host:
        return []
    port = extract_port_from_url(data_proxy_url)
    return [port] if port is not None else []


def build_egress_target_rules(data_proxy_url: str | None) -> tuple[list[str], list[str]]:
    """Return (allowed_domains, allowed_ips) for run-scoped data proxy egress."""
    host = extract_host_from_url(data_proxy_url)
    if not host:
        return [], []
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return [host], []
    return [], ["{}/{}".format(address.compressed, address.max_prefixlen)]


def _as_container_path(path: Path) -> str:
    raw = posix_path_string(path)
    if raw.startswith("/"):
        return raw.rstrip("/") or "/"
    return posix_path_string(path.resolve())


def _read_only_bind(path: Path) -> dict[str, str]:
    resolved = _as_container_path(path)
    return {
        "host_path": resolved,
        "sandbox_path": resolved,
        "mode": "ro",
    }


def _read_write_bind(path: Path) -> dict[str, str]:
    resolved = _as_container_path(path)
    return {
        "host_path": resolved,
        "sandbox_path": resolved,
        "mode": "rw",
    }


class OntoSandboxPolicyBuilder:
    """Build a jiuwenbox policy dict for one run-scoped sandbox."""

    @classmethod
    def build(cls, ctx: PolicyContext) -> dict:
        run_short = ctx.run_id.replace("-", "")[:8] or "run"
        configured_read_only_paths = ctx.read_only_paths or (ctx.workspace_root,)
        read_only_paths = tuple(
            dict.fromkeys(Path(path).resolve() for path in configured_read_only_paths)
        )
        bind_mounts: list[dict[str, str]] = [
            *(_read_only_bind(path) for path in read_only_paths),
            _read_only_bind(ctx.sandbox_runtime_python_dir),
        ]
        writable_paths = tuple(Path(path).resolve() for path in ctx.writable_paths)
        bind_mounts.extend(_read_write_bind(path) for path in writable_paths)
        bind_mounts.extend(_SYSTEM_BIND_MOUNTS)

        allowed_domains, allowed_ips = build_egress_target_rules(ctx.data_proxy_url)
        egress: dict = {
            "default": "deny",
            "allowed_ports": build_egress_allowed_ports(ctx.data_proxy_url),
            "blocked_ips": list(_BLOCKED_METADATA_IPS),
            "blocked_ports": [53],
            "allowed_domains": allowed_domains,
            "allowed_ips": allowed_ips,
        }

        return {
            "version": 1,
            "name": f"onto-script-{run_short}",
            "filesystem_policy": {
                "directories": [{"path": "/tmp", "permissions": "1777"}],
                "read_only": [
                    "/bin",
                    "/sbin",
                    "/usr",
                    "/usr/local",
                    "/lib",
                    "/lib64",
                    "/etc",
                    "/opt",
                    *(_as_container_path(path) for path in read_only_paths),
                    _as_container_path(ctx.sandbox_runtime_python_dir),
                ],
                "read_write": [
                    "/tmp",
                    *(_as_container_path(path) for path in writable_paths),
                ],
                "bind_mounts": bind_mounts,
                "device": [
                    {"host_path": "/dev/null", "sandbox_path": "/dev/null"},
                    {"host_path": "/dev/urandom", "sandbox_path": "/dev/urandom"},
                ],
            },
            "process": {
                "run_as_user": "sandbox",
                "run_as_group": "sandbox",
            },
            "namespace": {
                "user": True,
                "pid": True,
                "ipc": True,
                "cgroup": True,
                "uts": True,
            },
            "landlock": {"compatibility": "best_effort"},
            "network": {
                "mode": "isolated",
                "uplink": {"nat": True, "interface": ""},
                "egress": egress,
            },
            "cgroup": {
                "memory_max": ctx.memory_max,
                "cpu_max": ctx.cpu_max,
                "pids_max": ctx.pids_max,
            },
            "syscall": {
                "x86_64": {"blocked": list(_BLOCKED_SYSCALLS)},
                "arm64": {"blocked": list(_BLOCKED_SYSCALLS)},
            },
        }
