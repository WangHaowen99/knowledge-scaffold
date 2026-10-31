#!/usr/bin/env python3
"""
State helper — 单一文件管理所有命令的执行锁与进度快照。

所有状态保存在 `.ontology-state/` 下：
    lock-{command}.json     每个正在执行的命令的锁（含开始时间、阶段、PID、owner_pid）
    progress.json           各阶段完整进度快照（全体命令共用）

子命令：
    check-lock <command>                查询锁状态；输出 ok / locked:{ts}:{phase} /
                                        stale:{ts}:{phase}（owner 进程已死或锁超 24h）
    acquire <command> <phase>           写入锁；同会话（owner_pid 相同）覆盖以支持
                                        阶段切换；若另一活跃会话已持有则输出
                                        conflict:{ts}:{phase} 并退出码 2
    force-release <command>             强制删除锁（用户"清理重来"时使用）
    release <command>                   正常结束时释放锁
    update-progress <key> <value> …     键值对更新 progress.json
    show-progress                       打印 progress.json
    detect-stage                        扫描 ontology/ 目录，推断当前建模阶段
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import research_review

STATE_DIR = Path('.ontology-state')
PROGRESS_FILE = STATE_DIR / 'progress.json'

# Any lock older than this many hours is assumed to come from a crashed run.
STALE_LOCK_HOURS = 24


def _ensure_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {}
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}


def _save_progress(data: dict):
    _ensure_dir()
    PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _lock_path(command: str) -> Path:
    return STATE_DIR / f'lock-{command}.json'


def _lock_age_hours(ts_str: str):
    """Return age in hours for an ISO-ish timestamp, or None if unparseable."""
    try:
        t = time.mktime(time.strptime(ts_str, '%Y-%m-%d %H:%M:%S'))
        return (time.time() - t) / 3600
    except Exception:
        return None


def _is_pid_alive(pid) -> bool:
    """Best-effort POSIX check: is `pid` an existing process?

    Returns False for None/0/non-int input and ProcessLookupError; True when
    the process exists even if we lack permission to signal it (rare across
    same-user invocations, but conservatively keeps the lock).
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _get_session_pid() -> int:
    """Return the long-lived ancestor PID that represents this session.

    Why not just `os.getppid()`: in the Claude Code harness each Bash tool
    call spawns a fresh shell, so `os.getppid()` is the shell PID which
    dies the moment the Bash call returns. The shell's parent (one level
    up) is the Claude process itself, which persists across Bash calls.
    Storing THAT pid as the lock owner makes same-session re-acquires
    detect each other, and cross-session conflicts become real.

    Walks up exactly one level via `ps -o ppid= -p`. Falls back to the
    shell pid if `ps` is unavailable or the lookup fails.
    """
    shell_pid = os.getppid()
    try:
        result = subprocess.run(
            [shutil.which('ps') or '/bin/ps', '-o', 'ppid=', '-p', str(shell_pid)],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            parent = result.stdout.strip()
            if parent:
                parent_int = int(parent)
                if parent_int > 1:  # treat PID 1 (init/launchd) as not-our-session
                    return parent_int
    except Exception:
        pass
    return shell_pid


def _read_lock(path: Path):
    """Return parsed lock dict or None if missing/corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def check_lock(command: str) -> int:
    path = _lock_path(command)
    data = _read_lock(path)
    if data is None:
        # Either no lock, or a corrupt one. Distinguish so caller can react:
        # missing file → ok; corrupt JSON → treat as stale so callers recover.
        if path.exists():
            print('stale:unknown:unknown')
        else:
            print('ok')
        return 0
    ts = data.get('started_at', 'unknown')
    phase = data.get('phase', 'unknown')
    owner_pid = data.get('owner_pid')
    age = _lock_age_hours(ts)
    # Owner process gone → lock is orphaned regardless of age.
    if owner_pid is not None and not _is_pid_alive(owner_pid):
        print(f'stale:{ts}:{phase}')
        return 0
    if age is not None and age > STALE_LOCK_HOURS:
        print(f'stale:{ts}:{phase}')
    else:
        print(f'locked:{ts}:{phase}')
    return 0


def acquire(command: str, phase: str) -> int:
    """Write the lock for this command.

    Behavior:
    - No existing lock → create one.
    - Same session (owner_pid matches the long-lived ancestor returned by
      `_get_session_pid()`, typically the Claude process) → overwrite to
      update the phase in place (supports phase transitions across the
      multiple Bash calls that make up a single command run).
    - Existing lock from a dead owner / no owner_pid (legacy lock) / corrupt
      JSON → treat as stale and overwrite (auto-recover from crashes).
    - Existing lock from a different LIVE session → refuse with
      `conflict:{ts}:{phase}` on stdout, recovery hint on stderr, exit code 2.

    Returns 0 on success, 2 on conflict.
    """
    _ensure_dir()
    path = _lock_path(command)
    ppid = _get_session_pid()
    existing = _read_lock(path)
    if existing is not None:
        existing_owner = existing.get('owner_pid')
        # Same-session re-acquire (phase transition) is always permitted.
        if existing_owner is None or existing_owner == ppid or not _is_pid_alive(existing_owner):
            pass  # fall through to overwrite
        else:
            ts = existing.get('started_at', 'unknown')
            existing_phase = existing.get('phase', 'unknown')
            print(f'conflict:{ts}:{existing_phase}')
            print(
                f'另一活跃会话正在持有 {command} 锁'
                f'（owner_pid={existing_owner}, phase={existing_phase}, started={ts}）。\n'
                f'若确认无其他会话在跑，执行：'
                f'python3 .claude/scripts/state_helper.py force-release {command}',
                file=sys.stderr,
            )
            return 2
    payload = {
        'command': command,
        'phase': phase,
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'pid': os.getpid(),
        'owner_pid': ppid,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print('ok')
    return 0


def force_release(command: str) -> int:
    path = _lock_path(command)
    if path.exists():
        path.unlink()
    print('ok')
    return 0


def release(command: str) -> int:
    """Release the lock and snapshot current stage into progress.json.

    Doing it here (instead of leaving it to a separate update-progress call)
    guarantees progress.json always reflects the last completed command —
    commands no longer need to remember to pass explicit args.
    """
    path = _lock_path(command)
    if path.exists():
        path.unlink()
    data = _load_progress()
    data['last_command'] = command
    data['stage'] = _compute_stage()
    data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _save_progress(data)
    print('ok')
    return 0


def update_progress(pairs: list) -> int:
    """Update progress.json with key/value pairs.

    If called with zero args, auto-records the current stage (release() does
    this already, so zero-arg calls are mostly a timestamp touch).
    """
    data = _load_progress()
    if len(pairs) % 2 != 0:
        print('error:expect key value pairs', file=sys.stderr)
        return 1
    for i in range(0, len(pairs), 2):
        key, val = pairs[i], pairs[i + 1]
        data[key] = val
    if not pairs:
        data['stage'] = _compute_stage()
    data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _save_progress(data)
    print('ok')
    return 0


def show_progress() -> int:
    data = _load_progress()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


# 阶段推断
# 资料分析（research）保持启用，是常规建模起点之一。提取已拆为 object / operation
# 两个命令，阶段终点以 operation 文档存在为准。
# `资料分析待核验`：报告已落盘但没通过 research_checker 的内容验收。报告存在不等于
# 内容可用，所以它独立成一个阶段，不并入 `资料分析完成`（见 research_review.py）。
STAGE_ORDER = [
    ('未开始', '未开始'),
    ('资料分析待核验', '资料分析待核验'),
    ('资料分析完成', '资料分析完成'),
    ('流程图就绪', '流程图就绪'),
    ('对象已提取', '对象已提取'),
    ('逻辑已提取', '逻辑已提取'),
    ('校验待审阅', '校验待审阅'),
    ('完成', '完成'),
]


def _has_md_files(path: Path) -> bool:
    return path.exists() and any(f for f in path.rglob('*.md') if f.name.lower() != 'index.md')


def _has_drawio_files(path: Path) -> bool:
    return path.exists() and any(path.glob('*.drawio'))


# 每个命令的前置阶段要求。
# 注意：
# - extract-object 只要求"有资料分析报告或流程图之一"。`未开始` 状态会触发
#   跳步守卫，提示用户先画流程图或显式做资料分析（/ontology-research）。
# - extract-operation 要求对象已提取；逻辑提取完成即可校验、生成代码。
COMMAND_PREREQS = {
    'research': [],
    'flowchart': [],
    'extract-object': ['资料分析完成'],
    'extract-operation': ['对象已提取'],
    'validate': ['逻辑已提取', '对象已提取'],
    'mock': ['对象已提取'],
    'trace': ['逻辑已提取'],
    'codegen': ['逻辑已提取'],
    'codegen-scan': ['逻辑已提取'],
    'solver-evaluator': ['对象已提取'],
    'overview': ['对象已提取'],
    'impact': ['对象已提取'],
    'query': ['对象已提取'],
    'update': ['逻辑已提取', '对象已提取'],
    'evolution': ['对象已提取'],
}

STAGE_SORT = {
    '未开始': 0,
    '资料分析待核验': 1,
    '资料分析完成': 2,
    '流程图就绪': 3,
    '对象已提取': 4,
    '逻辑已提取': 5,
    '校验待审阅': 6,
    '完成': 7,
}

STAGE_NEXT_COMMAND = {
    '未开始': ('/ontology-research 或 /ontology-flowchart', '资料分析或直接画流程图'),
    '资料分析待核验': ('python3 .claude/scripts/research_checker.py', '核验资料分析报告内容后才能提取对象'),
    '资料分析完成': ('/ontology-flowchart 或 /ontology-extract-object', '可选画流程图或直接提取对象'),
    '流程图就绪': ('/ontology-extract-object', '提取业务对象'),
    '对象已提取': ('/ontology-extract-operation', '继续提取业务逻辑'),
    '逻辑已提取': ('/ontology-validate', '进入校验'),
}


def _compute_stage() -> str:
    research_has = _has_md_files(Path('research'))
    diagrams_has = _has_drawio_files(Path('ontology/diagrams'))
    objects_has = _has_md_files(Path('ontology/object'))
    operation_has = _has_md_files(Path('ontology/operation')) or _has_md_files(Path('ontology/logic'))
    reports_path = Path('reports')
    reports_has_pending = False
    if reports_path.exists():
        for f in reports_path.glob('*.md'):
            if '[ ]' in f.read_text(encoding='utf-8', errors='ignore'):
                reports_has_pending = True
                break
    progress = _load_progress()
    validate_done = bool(progress.get('validate_last_run'))
    # 高阶段优先：即便上游产物被删（/ontology-update 中断、用户手动清理），
    # 已抵达的最高阶段仍可见，状态机不再误退回 '未开始'。
    if operation_has and reports_has_pending:
        return '校验待审阅'
    if operation_has and validate_done:
        return '完成'
    if operation_has:
        return '逻辑已提取'
    if objects_has:
        return '对象已提取'
    if diagrams_has:
        return '流程图就绪'
    if research_has:
        # 报告以内容验收为准：没有有效回执时只到 `资料分析待核验`，不放行对象提取。
        return '资料分析完成' if research_review.accepted(Path('.')) else '资料分析待核验'
    return '未开始'


def check_prereq(command: str) -> int:
    """输出 ok 或 missing:{阶段},{阶段}"""
    reqs = COMMAND_PREREQS.get(command, [])
    stage = _compute_stage()
    current_level = STAGE_SORT.get(stage, 0)
    missing = []
    for req in reqs:
        req_level = STAGE_SORT.get(req, 0)
        if current_level < req_level:
            missing.append(req)
    if missing:
        print(f'missing:{",".join(missing)}')
        return 0
    print('ok')
    return 0


def detect_stage() -> int:
    print(_compute_stage())
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    args = sys.argv[2:]

    def need(usage):
        if not args:
            print(f'用法: state_helper.py {usage}', file=sys.stderr)
            return None
        return args[0]

    if cmd == 'check-lock':
        name = need('check-lock <command>')
        return 1 if name is None else check_lock(name)
    if cmd == 'acquire':
        name = need('acquire <command> [phase]')
        return 1 if name is None else acquire(name, args[1] if len(args) > 1 else 'default')
    if cmd == 'release':
        name = need('release <command>')
        return 1 if name is None else release(name)
    if cmd == 'force-release':
        name = need('force-release <command>')
        return 1 if name is None else force_release(name)
    if cmd == 'update-progress':
        return update_progress(args)
    if cmd == 'show-progress':
        return show_progress()
    if cmd == 'detect-stage':
        return detect_stage()
    if cmd == 'check-prereq':
        name = need('check-prereq <command>')
        return 1 if name is None else check_prereq(name)
    print(f'unknown subcommand: {cmd}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
