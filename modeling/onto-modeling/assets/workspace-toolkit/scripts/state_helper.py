#!/usr/bin/env python3
"""
State helper — 单一文件管理所有命令的执行锁与进度快照。

所有状态保存在 `.ontology-state/` 下：
    lock-{command}.json     每个正在执行的命令的锁（含开始时间、阶段、PID，
                            持有者为 owner（Web 后端托管的引擎实例）或 owner_pid）
    progress.json           各阶段完整进度快照（全体命令共用）
另有 `.onto/run/active.json`：正在执行的命令所属节点与写入范围（节点声明的 produces、writes），
acquire 时写、release 时删；内核守卫只放行这些路径（方案 D11）。分段建模时（后端在
`.onto/run/exec.json` 记了这一段）另记 `protect`：这一段以外、已经有了的条目，守卫不让改（详设 4.3）。

锁的持有者：Web 后端每次连接引擎时把实例标识写进 `.onto/run/engine.json`，本进程是
该后端的子孙时，持有者就是这个引擎实例——同一实例的多次 Bash 调用可续写锁，实例换了
（回收、重启、改配置、切引擎）旧锁即过期，与后端是不是 1 号进程、引擎是 CLI 子进程
还是进程内运行都无关。不在后端托管下（终端里直接跑 CLI）时退回按长寿祖先进程号判断。

子命令：
    check-lock <command>                查询锁状态；输出 ok / own:{ts}:{phase}（本会话自己拿的）/
                                        locked:{ts}:{phase} /
                                        stale:{ts}:{phase}（持有者已不在或锁超 24h）
    acquire <command> <phase>           写入锁；同会话（持有者相同）覆盖以支持
                                        阶段切换；若另一活跃会话已持有则输出
                                        conflict:{ts}:{phase} 并退出码 2
    force-release <command>             强制删除锁（用户"清理重来"时使用）
    release <command>                   正常结束时释放锁
    update-progress <key> <value> …     键值对更新 progress.json
    show-progress                       打印 progress.json
    detect-stage                        按主流程定义推断当前建模阶段
    check-prereq <command>              命令前置：ok 或 missing:{阶段},{阶段}
    next-step [阶段]                    /ontology 的「下一步建议」条目（缺省取当前阶段）
    suggest                             统一下一步（JSON：mode、primary、最多 2 个 secondary），与界面
                                        建议条、宿主注入同一份（后端写的 .onto/run/flow-state.json）

阶段、命令前置与下一步建议都来自主流程定义 `.claude/flow.yaml`（flow_state.py 解释），
本文件不再自带阶段表（方案 D12；命令模板按 detect-stage 的 8 个阶段名分支，名字不变）。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import blueprint
import flow_state
import onto_metamodel

STATE_DIR = Path('.ontology-state')
PROGRESS_FILE = STATE_DIR / 'progress.json'
# Web 后端写入的在线引擎实例标识（见模块说明）
ENGINE_MARKER = Path('.onto') / 'run' / 'engine.json'
# Web 后端开始执行一条命令时写的标记（节点、分段、步骤）
EXEC_MARKER = Path('.onto') / 'run' / 'exec.json'
# 正在执行的命令与它们的写入范围（节点声明的 produces、writes）：内核守卫只放行这些路径（方案 D11）
ACTIVE_FILE = Path('.onto') / 'run' / 'active.json'
ANCESTOR_SCAN_LIMIT = 32

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


def _parent_pid(pid: int):
    """父进程号；取不到返回 None。Linux 读 /proc（不起子进程），其余平台用 ps。"""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        # 第 2 列进程名可能含空格和括号：从最后一个 ')' 之后取
        return int(stat.rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(
            [shutil.which('ps') or '/bin/ps', '-o', 'ppid=', '-p', str(pid)],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def _ancestor_pids() -> list:
    pids = []
    pid = os.getpid()
    for _ in range(ANCESTOR_SCAN_LIMIT):
        parent = _parent_pid(pid)
        if not parent or parent in pids:
            break
        pids.append(parent)
        if parent <= 1:
            break
        pid = parent
    return pids


def _read_engine_marker():
    try:
        data = json.loads(ENGINE_MARKER.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get('engine_id'):
        return None
    return data


def _live_engine_owner():
    """工作区当前在线引擎实例的持有者标识；没有返回 None。"""
    data = _read_engine_marker()
    return f"engine:{data['engine_id']}" if data else None


def _session_owner():
    """本次调用所属会话：返回 (owner, owner_pid)，二者只有一个非空。

    本进程是写下 ENGINE_MARKER 的后端的子孙时，持有者是该引擎实例；在同一工作区里
    人工跑的脚本不是后端子孙，不会冒用引擎的锁，退回按长寿祖先进程号判断。
    """
    data = _read_engine_marker()
    if data:
        try:
            host = int(data.get('host_pid'))
        except (TypeError, ValueError):
            host = None
        if host and host in _ancestor_pids():
            return f"engine:{data['engine_id']}", None
    return None, _get_session_pid()


def _owner_alive(data: dict):
    """锁的持有者是否还在：True / False；不带持有者信息的旧锁返回 None。"""
    owner = data.get('owner')
    if owner:
        return owner == _live_engine_owner()
    owner_pid = data.get('owner_pid')
    if owner_pid is None:
        return None
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError):
        return False
    # 1 号进程从来不是会话：要么是 init/tini，要么是没加 init 的容器里的后端自己
    # （那时 Jiuwen 引擎写下的锁持有者就是 1，按孤儿锁处理）
    if owner_pid <= 1:
        return False
    return _is_pid_alive(owner_pid)


def _owner_label(data: dict) -> str:
    return data.get('owner') or f"owner_pid={data.get('owner_pid')}"


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
    age = _lock_age_hours(ts)
    # Owner gone → lock is orphaned regardless of age.
    if _owner_alive(data) is False:
        print(f'stale:{ts}:{phase}')
        return 0
    owner, _ = _session_owner()
    if owner and data.get('owner') == owner:
        # 本会话自己拿的锁（上下文压缩后记不清了再查到的就是它）：接着做，不当残留清掉
        print(f'own:{ts}:{phase}')
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
    - Same session (same engine instance under the Web backend, otherwise
      owner_pid matches the long-lived ancestor returned by
      `_get_session_pid()`, typically the Claude process) → overwrite to
      update the phase in place (supports phase transitions across the
      multiple Bash calls that make up a single command run).
    - Existing lock from a dead owner / no owner info (legacy lock) / corrupt
      JSON → treat as stale and overwrite (auto-recover from crashes).
    - Existing lock from a different LIVE session → refuse with
      `conflict:{ts}:{phase}` on stdout, recovery hint on stderr, exit code 2.

    Returns 0 on success, 2 on conflict.
    """
    _ensure_dir()
    path = _lock_path(command)
    owner, ppid = _session_owner()
    existing = _read_lock(path)
    if existing is not None:
        same_session = (
            existing.get('owner') == owner if owner
            else not existing.get('owner') and existing.get('owner_pid') == ppid
        )
        # Same-session re-acquire (phase transition) is always permitted.
        if same_session or _owner_alive(existing) is not True:
            pass  # fall through to overwrite
        else:
            ts = existing.get('started_at', 'unknown')
            existing_phase = existing.get('phase', 'unknown')
            print(f'conflict:{ts}:{existing_phase}')
            print(
                f'另一活跃会话正在持有 {command} 锁'
                f'（{_owner_label(existing)}, phase={existing_phase}, started={ts}）。\n'
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
    }
    if owner:
        payload['owner'] = owner
    else:
        payload['owner_pid'] = ppid
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    _mark_active(command, phase)
    print('ok')
    return 0


# ---- 写入范围（方案 D11）----


def _load_active() -> dict:
    try:
        data = json.loads(ACTIVE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'commands': {}}
    if not isinstance(data, dict) or not isinstance(data.get('commands'), dict):
        return {'commands': {}}
    return data


def _save_active(data: dict) -> None:
    if not data['commands']:
        ACTIVE_FILE.unlink(missing_ok=True)
        return
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_FILE.with_name(f'.active.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, ACTIVE_FILE)


def _node_patterns(node: dict) -> list:
    allow = []
    for key in ('produces', 'writes'):
        for item in node.get(key) or []:
            pattern = str(item.get('path') if isinstance(item, dict) else item)
            if pattern and pattern not in allow:
                allow.append(pattern)
    return allow


def _slice_node(flow: dict, command: str):
    """后端开始执行时记的是按段的节点、这条命令是它的一步（如带分段的规则提取、场景走查）时返回那个节点。"""
    try:
        marker = json.loads(EXEC_MARKER.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    node = next((item for item in flow.get('nodes') or [] if item.get('id') == (marker or {}).get('node')), None)
    if node is None or not node.get('per_slice'):
        return None
    commands = {str(step.get('command') or '').lstrip('/').removeprefix('ontology-') for step in node.get('steps') or []}
    return node if command in commands else None


def _command_scope(command: str) -> tuple:
    """命令所属节点与它的写入范围（produces + writes）；不属于任何节点时为 (None, None)：不限范围。

    这一次是按段的节点的一步时，节点取按段的那个、范围是两者的并集（规则提取还会写指标，场景走查写场景文件）。"""
    try:
        flow = _flow()
    except (OSError, ValueError):
        return None, None
    node_id = flow_state.command_node(flow, command)
    node = next((item for item in flow.get('nodes') or [] if item.get('id') == node_id), None)
    sliced = _slice_node(flow, command)
    if sliced is not None:
        allow = _node_patterns(node) if node is not None else []
        allow += [pattern for pattern in _node_patterns(sliced) if pattern not in allow]
        return sliced['id'], allow
    if node is None:
        return None, None
    return node_id, _node_patterns(node)


def _changed_here() -> set:
    """这份副本里相对主线分叉点改过的文件（含没提交、没跟踪的）：这一轮自己建的、改的不算段外。"""
    try:
        head = str(json.loads(FLOW_STATE_FILE.read_text(encoding='utf-8')).get('mainline_head') or '')
    except (OSError, ValueError):
        head = ''
    base = ((_git_quiet('merge-base', 'HEAD', head) or '').strip() if head else '') or 'HEAD'
    changed = (_git_quiet('-c', 'core.quotepath=false', 'diff', '--name-only', base) or '').splitlines()
    changed += (_git_quiet('-c', 'core.quotepath=false', 'ls-files', '--others', '--exclude-standard') or '').splitlines()
    return {line.strip() for line in changed if line.strip()}


def _slice_protect(node_id: str, allow: list) -> tuple:
    """分段建模：(分段, 段外已有的条目)。不是按段的节点、全量模式、不知道哪一段时 (None, [])。"""
    slice_id = blueprint.current_slice()
    if not slice_id or slice_id == blueprint.FULL_SLICE_ID:
        return None, []
    try:
        flow = _flow()
        node = next((item for item in flow.get('nodes') or [] if item.get('id') == node_id), None)
        doc = blueprint.load(Path('.'))
    except (OSError, ValueError):
        return None, []
    item = blueprint.find_slice(doc, slice_id)
    if node is None or not node.get('per_slice') or blueprint.mode(doc) != 'sliced' or item is None:
        return None, []
    own = set(blueprint.member_paths(item))
    # 这一段的场景（一个操作一份）也是这一段的
    own |= {f'ontology/testcase/{onto_metamodel.encode_filename(name)}.md' for name in item.get('operations') or []}
    changed = _changed_here()
    protect = []
    for pattern in allow:
        directory = pattern.rsplit('/', 1)[0] if pattern.endswith('/*') else ''
        if not directory.startswith('ontology/') or not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob('*.md')):
            rel = path.as_posix()
            if path.name.lower() != 'index.md' and rel not in own and rel not in changed and rel not in protect:
                protect.append(rel)
    return slice_id, protect


def _mark_active(command: str, phase: str) -> None:
    node_id, allow = _command_scope(command)
    data = _load_active()
    spec = {'node': node_id, 'allow': allow, 'phase': phase, 'since': time.strftime('%Y-%m-%d %H:%M:%S')}
    if node_id and allow:
        slice_id, protect = _slice_protect(node_id, allow)
        if slice_id:
            spec.update(slice=slice_id, protect=protect)
    data['commands'][command] = spec
    try:
        _save_active(data)
    except OSError:
        pass  # 写不了时守卫按不限范围处理，不挡命令


def _clear_active(command: str) -> None:
    data = _load_active()
    if command in data['commands']:
        data['commands'].pop(command)
        try:
            _save_active(data)
        except OSError:
            pass


def force_release(command: str) -> int:
    path = _lock_path(command)
    if path.exists():
        path.unlink()
    _clear_active(command)
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
    _clear_active(command)
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


# 阶段推断：全部按主流程定义（flow.yaml）计算，见 flow_state.py。
# `资料分析待核验`：报告已落盘但没通过 research_checker 的内容验收。报告存在不等于
# 内容可用，所以它独立成一个阶段，不并入 `资料分析完成`（见 research_review.py）。
_FLOW = None


def _flow() -> dict:
    global _FLOW
    if _FLOW is None:
        _FLOW = flow_state.load_flow()
    return _FLOW


def _compute_stage() -> str:
    # 高阶段优先：即便上游产物被删（/ontology-update 中断、用户手动清理），
    # 已抵达的最高阶段仍可见，状态机不再误退回 '未开始'。
    return flow_state.legacy_stage(_flow(), Path('.'))


def check_prereq(command: str) -> int:
    """输出 ok 或 missing:{阶段},{阶段}"""
    missing = flow_state.missing_prereqs(_flow(), command, _compute_stage())
    if missing:
        print(f'missing:{",".join(missing)}')
        return 0
    print('ok')
    return 0


def next_step(stage: str | None = None) -> int:
    """逐行打印某阶段的「下一步建议」条目（/ontology 原样展示）。"""
    for line in flow_state.next_step_lines(_flow(), stage or _compute_stage()):
        print(line)
    return 0


def detect_stage() -> int:
    print(_compute_stage())
    return 0


# ---- 统一下一步（方案 D15、D16；详设 6.10）----

FLOW_STATE_FILE = Path('.onto') / 'run' / 'flow-state.json'


def _git_quiet(*args) -> str | None:
    try:
        result = subprocess.run(['git', *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _local_changes(mainline_head: str) -> bool:
    """这份副本里有这一轮还没合入的改动（含没提交的、没跟踪的文件）。

    和副本与主线的分叉点比，不和主线最新比：这一轮里你在网页上的保存让主线前进了，副本没有那些改动，
    不能当成副本改了东西。资料区与锁文件不算。"""
    if not mainline_head:
        return False
    base = (_git_quiet('merge-base', 'HEAD', mainline_head) or '').strip() or mainline_head
    diff = _git_quiet('diff', '--name-only', base, '--', '.', ':(exclude).onto')
    untracked = _git_quiet('ls-files', '--others', '--exclude-standard')
    return bool((diff or '').strip() or (untracked or '').strip())


def _locked_nodes(flow: dict) -> set:
    nodes = set()
    for path in STATE_DIR.glob('lock-*.json') if STATE_DIR.is_dir() else []:
        node = flow_state.command_node(flow, path.name[len('lock-'):-len('.json')])
        if node:
            nodes.add(node)
    return nodes


def suggest_payload() -> dict:
    """下一步建议（契约 7.8 Suggestions）：节点状态取后端写的 `.onto/run/flow-state.json`（执行记录、变更集、
    指纹算出的那份），再叠上这份副本里这一轮还没合入的改动；算法与后端、宿主注入是同一个 flow_state.suggest。"""
    flow = _flow()
    try:
        data = json.loads(FLOW_STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    states = {key: dict(value) for key, value in (data.get('nodes') or {}).items() if isinstance(value, dict)}
    context = dict(data.get('context') or {})
    root = Path('.')
    probes = flow_state.evaluate(flow, root)
    hits = flow_state.node_hits(flow, root, probes)
    context['stage'] = flow_state.legacy_stage(flow, root, probes)
    changed = _local_changes(str(data.get('mainline_head') or ''))
    flow_state.overlay_pending(flow, states, hits if changed else {}, _locked_nodes(flow))
    if changed:
        changeset = context.get('changeset') or {'id': 'current'}
        context['changeset'] = {**changeset, 'state': 'pending_confirm'}
    return flow_state.suggest(flow, states, context)


def suggest() -> int:
    """输出 JSON（1 个主推，最多 2 个次选）；命令收尾按它渲染「下一步建议」，最多 3 条。"""
    print(json.dumps(suggest_payload(), ensure_ascii=False))
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
    if cmd == 'next-step':
        return next_step(args[0] if args else None)
    if cmd == 'suggest':
        return suggest()
    print(f'unknown subcommand: {cmd}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
