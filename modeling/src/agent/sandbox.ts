import { randomUUID } from 'node:crypto';
import { request as httpRequest } from 'node:http';

import { config } from '../config.js';

const MAX_SCRIPT_BYTES = 64 * 1024;
const RESULT_PREFIX = '__MODELING_RESULT__';

export interface ScriptCaseResult {
  index: number;
  passed: boolean;
  actual?: unknown;
  expected: unknown;
  error?: string;
}

export interface ScriptTestResult {
  total: number;
  passed: number;
  failed: number;
  results: ScriptCaseResult[];
  sandbox: 'jiuwenbox';
}

interface JiuwenboxExecResult {
  exit_code?: number;
  stdout?: string;
  stderr?: string;
}

export async function runScriptCasesInJiuwenbox(input: {
  runId: string;
  source: string;
  cases: Record<string, unknown>[];
  socketPath?: string;
}): Promise<ScriptTestResult> {
  if (Buffer.byteLength(input.source, 'utf8') > MAX_SCRIPT_BYTES) {
    throw new Error(`脚本超过沙箱测试上限 ${MAX_SCRIPT_BYTES} 字节`);
  }
  const socketPath = input.socketPath ?? config.jiuwenboxSocketPath;
  const sandboxId = `mdl-${randomUUID().replaceAll('-', '').slice(0, 11)}`;
  let created = false;
  try {
    await requestJson(socketPath, 'POST', '/api/v1/sandboxes', {
      sandbox_id: sandboxId,
      policy_mode: 'override',
      policy: sandboxPolicy(input.runId),
    });
    created = true;
    const response = await requestJson<JiuwenboxExecResult>(
      socketPath,
      'POST',
      `/api/v1/sandboxes/${encodeURIComponent(sandboxId)}/exec`,
      {
        command: [
          '/usr/bin/python3',
          '-I',
          '-c',
          SCRIPT_CASE_RUNNER,
          Buffer.from(input.source, 'utf8').toString('base64'),
          Buffer.from(JSON.stringify(input.cases), 'utf8').toString('base64'),
        ],
        workdir: '/tmp',
        env: {
          LANG: 'C.UTF-8',
          LC_ALL: 'C.UTF-8',
          PYTHONDONTWRITEBYTECODE: '1',
          PYTHONHASHSEED: '0',
        },
        timeout_seconds: 30,
      },
      50_000,
    );
    if (Number(response.exit_code) !== 0) {
      throw new Error(trimDiagnostic(response.stderr || response.stdout || `exit ${response.exit_code}`));
    }
    const line = String(response.stdout ?? '').split(/\r?\n/).reverse()
      .find((item: string) => item.startsWith(RESULT_PREFIX));
    if (!line) throw new Error(`沙箱没有返回结构化测试结果：${trimDiagnostic(response.stderr || response.stdout || '无输出')}`);
    const parsed = JSON.parse(line.slice(RESULT_PREFIX.length)) as ScriptCaseResult[];
    if (!Array.isArray(parsed) || parsed.length !== input.cases.length) {
      throw new Error('沙箱返回的测试结果数量与契约不一致');
    }
    const passed = parsed.filter((item) => item.passed === true).length;
    return {
      total: parsed.length,
      passed,
      failed: parsed.length - passed,
      results: parsed,
      sandbox: 'jiuwenbox',
    };
  } finally {
    if (created) {
      await requestJson(socketPath, 'DELETE', `/api/v1/sandboxes/${encodeURIComponent(sandboxId)}`)
        .catch(() => undefined);
    }
  }
}

function requestJson<T = Record<string, unknown>>(
  socketPath: string,
  method: string,
  path: string,
  payload?: unknown,
  timeoutMs = 15_000,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const body = payload === undefined ? undefined : Buffer.from(JSON.stringify(payload));
    const request = httpRequest({
      socketPath,
      method,
      path,
      headers: body ? {
        'Content-Type': 'application/json',
        'Content-Length': String(body.length),
      } : undefined,
      timeout: timeoutMs,
    }, (response) => {
      const chunks: Buffer[] = [];
      response.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
      response.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        if ((response.statusCode ?? 500) >= 400) {
          reject(new Error(`JiuwenBox ${method} ${path} 返回 ${response.statusCode}：${trimDiagnostic(raw)}`));
          return;
        }
        if (!raw.trim()) {
          resolve({} as T);
          return;
        }
        try {
          resolve(JSON.parse(raw) as T);
        } catch {
          reject(new Error(`JiuwenBox 返回了无效 JSON：${trimDiagnostic(raw)}`));
        }
      });
    });
    request.on('timeout', () => request.destroy(new Error('JiuwenBox 请求超时')));
    request.on('error', reject);
    if (body) request.write(body);
    request.end();
  });
}

function sandboxPolicy(runId: string) {
  const systemPaths = ['/bin', '/sbin', '/usr', '/usr/local', '/lib', '/lib64'];
  return {
    version: 1,
    name: `modeling-script-${runId.replace(/[^a-zA-Z0-9]/g, '').slice(0, 8) || 'run'}`,
    filesystem_policy: {
      directories: [{ path: '/tmp', permissions: '1777' }],
      read_only: [...systemPaths, '/etc'],
      read_write: ['/tmp'],
      bind_mounts: systemPaths.map((path) => ({ host_path: path, sandbox_path: path, mode: 'ro' })),
      device: [
        { host_path: '/dev/null', sandbox_path: '/dev/null' },
        { host_path: '/dev/urandom', sandbox_path: '/dev/urandom' },
      ],
    },
    process: { run_as_user: 'sandbox', run_as_group: 'sandbox' },
    namespace: { user: true, pid: true, ipc: true, cgroup: true, uts: true },
    landlock: { compatibility: 'best_effort' },
    network: {
      mode: 'isolated',
      uplink: { nat: false, interface: '' },
      egress: {
        default: 'deny',
        allowed_ports: [],
        blocked_ips: ['169.254.169.254/32'],
        blocked_ports: [53],
        allowed_domains: [],
        allowed_ips: [],
      },
    },
    cgroup: { memory_max: '256M', cpu_max: 1, pids_max: 64 },
    syscall: {
      x86_64: { blocked: BLOCKED_SYSCALLS },
      arm64: { blocked: BLOCKED_SYSCALLS },
    },
  };
}

function trimDiagnostic(value: string): string {
  const normalized = String(value || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '').trim();
  return normalized.slice(0, 2_000) || '未知错误';
}

const BLOCKED_SYSCALLS = [
  'kexec_load', 'kexec_file_load', 'reboot', 'pivot_root', 'swapon', 'swapoff',
  'nfsservctl', 'lookup_dcookie', 'ptrace', 'mount', 'umount2', 'unshare', 'setns',
  'userfaultfd', 'perf_event_open', 'bpf', 'add_key', 'request_key', 'keyctl',
  'io_uring_setup', 'io_uring_enter', 'io_uring_register', 'open_by_handle_at',
  'name_to_handle_at', 'init_module', 'finit_module', 'delete_module', 'acct',
];

const SCRIPT_CASE_RUNNER = String.raw`
import ast
import base64
import json
import runpy
import subprocess
import sys
import traceback

PREFIX = "__MODELING_RESULT__"
source = base64.b64decode(sys.argv[1]).decode("utf-8")
cases = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
path = "/tmp/modeling_candidate.py"
with open(path, "w", encoding="utf-8") as handle:
    handle.write(source)

tree = ast.parse(source, filename=path)
functions = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")]
force_cli = all(isinstance(case.get("argv"), list) for case in cases)
namespace = None if force_cli or not functions else runpy.run_path(path, run_name="modeling_contract")

def invoke(case):
    if namespace is not None:
        requested = str(case.get("function") or "").strip()
        candidates = [requested] if requested else ["run", "main", "execute", "calculate"] + functions
        function = next((namespace.get(name) for name in candidates if callable(namespace.get(name))), None)
        if function is None:
            raise RuntimeError("没有找到可测试的公开函数")
        value = case.get("input")
        if isinstance(value, dict):
            return function(**value)
        if isinstance(value, list):
            return function(*value)
        return function(value)

    argv = case.get("argv")
    if not isinstance(argv, list):
        argv = []
        value = case.get("input")
        if not isinstance(value, dict):
            raise RuntimeError("命令行脚本用例的 input 必须是对象，或显式提供 argv")
        for key, item in value.items():
            flag = "--" + str(key).replace("_", "-")
            if isinstance(item, bool):
                if item:
                    argv.append(flag)
            else:
                argv.extend([flag, json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)])
    completed = subprocess.run(
        [sys.executable, "-I", path, *[str(item) for item in argv]],
        cwd="/tmp", capture_output=True, text=True, timeout=20, check=False,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"},
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"exit {completed.returncode}")[-2000:])
    output = next((line for line in reversed(completed.stdout.splitlines()) if line.strip()), "")
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output

results = []
for index, case in enumerate(cases):
    expected = case.get("expected")
    try:
        actual = invoke(case)
        json.dumps(actual, ensure_ascii=False)
        results.append({"index": index, "passed": actual == expected, "actual": actual, "expected": expected})
    except BaseException as error:
        results.append({
            "index": index,
            "passed": False,
            "expected": expected,
            "error": (str(error) or error.__class__.__name__)[:2000],
        })

print(PREFIX + json.dumps(results, ensure_ascii=False, separators=(",", ":")))
`;
