"""Independent-evaluation results of a modeling project's published versions.

The evaluation service owns runs and artifacts. Modeling keeps only a status index per Hub package and
evaluation-service owner, fed by the service's notifications and replaced by the service's own view when
reconciled. A shared package is evaluated by several users whose runs are private to each of them, so one
user's runs never enable another user's buttons. The owner of the signed-in user is learned from the
service when reconciling, rather than derived locally from an issuer the two services may configure
differently. Reports, optimization suggestions and the full answers they link to are read with the user's own
login when viewed, and copied into the workspace only when the user starts an evaluation optimization of the
version the project is currently on.

Runs come from two origins: evaluation runs and runs built from consumer feedback; the index keeps them apart, one
entry per release and origin. After an evaluation optimization, publishing a new version is written back to the
evaluation service as the result of that run, with the optimizing user's login: at once when the publication had
that login, otherwise the next time the user opens the published versions.
"""
import hashlib
import json
import re
import time
from typing import Literal
import urllib.parse
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import eval_client, eval_data_files, hub_packages, knowledge_hub
from .workspace import WorkspaceError, _git, remove_tree

ID_PATTERN = r'^[A-Za-z0-9_-]{1,128}$'
RUN_PATTERN = r'^[a-f0-9]{32}$'
STEM_PATTERN = r'^eval-\d{8}-\d{6}-[a-f0-9]{6}$'
# answers.json 中的结果 ID：报告里第 N 个用例为 case-N
RESULT_PATTERN = r'^case-[1-9][0-9]{0,5}$'
STATUS_PATTERN = r'^[a-z_]{1,32}$'
# 与评测服务一致的表名、字段名规则，也保证表名可以直接作文件名、字段名无需 CSV 转义
TABLE_NAME = re.compile(r'[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]{0,63}')
VIEW_LIMIT = 2 * 1024 * 1024
ARTIFACT_LIMITS = {'report.md': 8 * 1024 * 1024, 'suggestions.md': 256 * 1024,
                   'answers.json': 8 * 1024 * 1024, 'context.json': 2 * 1024 * 1024}
STATE_DIR = ('.ontology-state', 'eval-service')
TABLES_FOLDER = 'eval-service'


class _Model(BaseModel):
    # 两个服务独立部署，被拒收的通知不再重试：未知字段忽略，已知字段严格校验
    model_config = ConfigDict(extra='ignore')


class Artifacts(_Model):
    revision: int = Field(ge=0, le=10**9)
    status: Literal['generating', 'ready', 'failed']
    stem: str | None = Field(default=None, pattern=STEM_PATTERN)
    generated_at: float | None = None
    report: Literal['pending', 'ready', 'failed']
    suggestions: Literal['pending', 'generating', 'ready', 'failed']
    suggestions_error: str | None = Field(default=None, max_length=4000)
    suggestions_model: str | None = Field(default=None, max_length=200)


class Summary(_Model):
    total: int = Field(ge=0)
    passed: int = Field(ge=0, alias='pass')
    partial: int = Field(ge=0)
    fail: int = Field(ge=0)
    error: int = Field(ge=0)
    manual: int = Field(ge=0)
    valid: int = Field(ge=0)
    score: int | None = Field(default=None, ge=0, le=100)
    result_status: Literal['complete', 'partial', 'unavailable']


class Outcome(_Model):
    outcome: str = Field(pattern=STATUS_PATTERN)
    release_id: str | None = Field(default=None, pattern=ID_PATTERN)
    version: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    reported_at: float | None = None


class Optimization(_Model):
    """How modeling handled a run's suggestions, as the evaluation service records it.

    Statuses are kept as text, so a new status of the service never makes a notification rejected.
    """
    status: str = Field(pattern=STATUS_PATTERN)
    updated_at: float | None = None
    reason: str | None = Field(default=None, max_length=2000)
    result: Outcome | None = None
    history: list[Outcome] = Field(default_factory=list, max_length=50)


ORIGINS = {'evaluation': 'releases', 'consumer_feedback': 'feedback'}


class Entry(_Model):
    release_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(min_length=1, max_length=64)
    run_id: str = Field(pattern=RUN_PATTERN)
    status: str = Field(pattern=STATUS_PATTERN)
    created_at: float | None = None
    finished_at: float | None = None
    artifacts: Artifacts
    summary: Summary
    # Runs of evaluation services before data-flywheel support carry neither.
    origin: str = Field(default='evaluation', pattern=STATUS_PATTERN)
    optimization: Optimization | None = None


def bucket(origin):
    """The index section of an origin; unknown origins of newer evaluation services are not shown."""
    return ORIGINS.get(origin)


class Notification(Entry):
    event_id: str = Field(max_length=64)
    sequence: int = Field(ge=0, le=10**12)
    owner: str = Field(pattern=ID_PATTERN)
    # knowledge_hub.identifier 不接受连字符开头：这样的包在建模侧不可能被关联
    ontology_id: str = Field(pattern=r'^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$')

    @model_validator(mode='after')
    def _consistent_event(self):
        if self.event_id != f'{self.run_id}:{self.sequence}':
            raise ValueError('event_id does not match run_id and sequence')
        return self


class Active(_Model):
    run_id: str = Field(pattern=RUN_PATTERN)
    release_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern=STATUS_PATTERN)


def _invalid_response():
    return WorkspaceError('评测服务响应格式错误', 502, 'eval_invalid_response')


def _not_found():
    return WorkspaceError('评测记录不存在或无权访问', 404, 'eval_not_found')


# ---- 状态索引 ----


def _index_path(ontology_id, owner):
    return (knowledge_hub.state_dir() / 'evaluations' / knowledge_hub.identifier(ontology_id)
            / f'{knowledge_hub.identifier(owner)}.json')


def _index_lock(ontology_id, owner):
    # 锁名同样受 identifier 长度限制，而包 ID 与所有者合计可达 256
    return knowledge_hub.lock('evaluations-' + hashlib.sha256(f'{ontology_id}/{owner}'.encode()).hexdigest()[:32])


def _read_index(ontology_id, owner):
    value = knowledge_hub.load(_index_path(ontology_id, owner), {}) if owner else {}
    return {'ontology_id': ontology_id, 'active': value.get('active'), 'releases': value.get('releases', {}),
            'feedback': value.get('feedback', {}), 'reconciled_at': value.get('reconciled_at')}


def _owner_path(subject):
    return knowledge_hub.state_dir() / 'evaluation-owners' / f'{hashlib.sha256(subject.encode()).hexdigest()[:32]}.json'


def _owner_of(subject):
    """The evaluation-service owner of this signed-in user, as last reported by the service."""
    value = knowledge_hub.load(_owner_path(subject), {})
    owner = value.get('owner') if isinstance(value, dict) else None
    return owner if isinstance(owner, str) and re.fullmatch(ID_PATTERN, owner) else None


def _entry(model):
    return model.model_dump(include=set(Entry.model_fields), by_alias=True)


def _moment(entry):
    # 中断的轮次没有 finished_at，以报告生成时间排序
    return entry.get('finished_at') or entry['artifacts'].get('generated_at') or 0


def _progress(entry):
    # 同一轮次的产物按修订号前进，同一修订内“生成中”先于终态；对账写入的条目没有通知序号
    artifacts = entry['artifacts']
    return artifacts['revision'], artifacts['status'] != 'generating', entry['sequence']


def _stale(stored, entry):
    if stored['run_id'] == entry['run_id']:
        return _progress(stored) >= _progress(entry)
    return (_moment(stored), stored['sequence']) > (_moment(entry), entry['sequence'])


def receive(notification):
    """Apply one notification durably to its owner's index; duplicates and late deliveries change nothing."""
    entry = {**_entry(notification), 'sequence': notification.sequence, 'received_at': time.time()}
    package_id, owner = notification.ontology_id, notification.owner
    section = bucket(notification.origin)
    if not section:
        return
    with _index_lock(package_id, owner):
        index = _read_index(package_id, owner)
        stored = index[section].get(notification.release_id)
        if stored and _stale(stored, entry):
            return
        # 与评测服务的口径一致：版本条目只记录报告已生成的最近一次评测
        if notification.artifacts.report == 'ready':
            index[section][notification.release_id] = entry
        if (index['active'] or {}).get('run_id') == notification.run_id:
            index['active'] = None
        knowledge_hub.save(_index_path(package_id, owner), index)


def reconcile(package_id, subject, token):
    """Replace the caller's index with the evaluation service's view, so deleted runs disappear from it."""
    started = time.time()
    # 两种来源都要：评测轮次，以及包负责人确认过的消费反馈轮次；旧版评测服务忽略 origin，只返回评测轮次
    query = urllib.parse.urlencode({'ontology_id': package_id, 'origin': 'all'})
    value = eval_client.call('GET', f'/package-evaluations?{query}', token)
    if not isinstance(value, dict) or not isinstance(value.get('releases'), list):
        raise _invalid_response()
    owner = value.get('owner')
    if not isinstance(owner, str) or not re.fullmatch(ID_PATTERN, owner):
        raise _invalid_response()
    try:
        fetched = [_entry(Entry.model_validate(item)) for item in value['releases']]
        active = Active.model_validate(value['active']).model_dump() if value.get('active') else None
    except ValidationError as exc:
        raise _invalid_response() from exc
    if _owner_of(subject) != owner:
        knowledge_hub.save(_owner_path(subject), {'owner': owner})
    with _index_lock(package_id, owner):
        index = _read_index(package_id, owner)
        sections = {section: {} for section in ORIGINS.values()}
        for entry in fetched:
            section = bucket(entry['origin'])
            if not section:
                continue
            stored = index[section].get(entry['release_id'])
            same = bool(stored) and stored['run_id'] == entry['run_id']
            sections[section][entry['release_id']] = {**entry, 'sequence': stored['sequence'] if same else 0,
                                                      'received_at': stored['received_at'] if same else 0}
        # 查询期间到达的通知比查询结果新：保留它，否则已结束的任务会一直显示评测中
        for section, entries in sections.items():
            for release_id, stored in index[section].items():
                if stored['received_at'] >= started:
                    entries[release_id] = stored
                    if active and active['run_id'] == stored['run_id']:
                        active = None
        index.update(**sections, active=active, reconciled_at=time.time())
        knowledge_hub.save(_index_path(package_id, owner), index)
        return index


# ---- 项目视图 ----


def overview(pid, subject, token, refresh=False):
    linked = hub_packages.association(pid, subject) or {}
    result = dict(configured=eval_client.configured(), reachable=None, message=None,
                  ontology_id=linked.get('ontology_id'), current_release_id=linked.get('release_id'),
                  current_version=linked.get('version'), active=None, releases={}, feedback={}, writeback=None)
    if not result['configured']:
        return {**result, 'message': '未配置评测服务地址'}
    if not linked:
        return result
    if refresh:
        # 先补上未完成的回写，随后的对账就能看到评测服务记下的处理结果；回写失败不影响查看
        try:
            settle(pid, subject, token)
        except (OSError, ValueError, WorkspaceError):
            pass
    # 还没对账过的用户不知道自己在评测服务的所有者标识，只能等对账，不能借用别人的索引
    index = _read_index(linked['ontology_id'], _owner_of(subject))
    if refresh:
        try:
            index = reconcile(linked['ontology_id'], subject, token)
            result['reachable'] = True
        except WorkspaceError:
            result.update(reachable=False, message='评测服务暂不可用，显示的是最近一次同步的评测状态')

    def shown(section):
        return {release_id: {key: value for key, value in entry.items() if key not in ('sequence', 'received_at')}
                for release_id, entry in index[section].items()}
    return {**result, 'active': index['active'], 'releases': shown('releases'), 'feedback': shown('feedback'),
            'writeback': writeback_view(pid, subject)}


def _reference(run, key):
    value = run.get(key)
    return value if isinstance(value, dict) else {}


def _linked_run(pid, subject, token, run_id):
    """The run, only when it evaluated this project's Hub package."""
    linked = hub_packages.association(pid, subject)
    if not linked:
        raise WorkspaceError('尚未关联个人 Hub 包', 404)
    if not re.fullmatch(RUN_PATTERN, run_id):
        raise _not_found()
    run = eval_client.call('GET', f'/runs/{run_id}', token)
    if not isinstance(run, dict):
        raise _invalid_response()
    package_id = _reference(run, 'base_ref').get('ontology_id') or _reference(run, 'package_ref').get('ontology_id')
    if package_id != linked['ontology_id']:
        raise _not_found()
    return run, linked


def _artifacts(run):
    try:
        return Artifacts.model_validate(run['artifacts']) if run.get('artifacts') else None
    except ValidationError as exc:
        raise _invalid_response() from exc


def _download(run_id, name, token):
    data, headers = eval_client.request('GET', f'/runs/{run_id}/artifacts/{name}', token, limit=ARTIFACT_LIMITS[name])
    return data, headers.get('x-artifact-revision', ''), headers.get('x-artifact-stem', '')


def _bounded_text(data):
    if len(data) <= VIEW_LIMIT:
        return data.decode('utf-8', errors='replace'), False
    cut = data.rfind(b'\n', 0, VIEW_LIMIT)
    return data[:cut if cut > 0 else VIEW_LIMIT].decode('utf-8', errors='ignore'), True


def document(pid, subject, token, run_id, kind):
    """The report or suggestions of one run for display: at most 2 MiB, cut at a line boundary."""
    run, _ = _linked_run(pid, subject, token, run_id)
    artifacts = _artifacts(run)
    reference = _reference(run, 'base_ref') or _reference(run, 'package_ref')
    result = dict(run_id=run_id, release_id=reference.get('release_id'), version=reference.get('version'),
                  revision=artifacts.revision if artifacts else None, content='', truncated=False)
    if kind == 'report' and (not artifacts or artifacts.report != 'ready'):
        raise WorkspaceError('评测报告尚未生成', 409, 'artifacts_not_ready')
    if kind == 'suggestions':
        status = artifacts.suggestions if artifacts else 'pending'
        result.update(status=status, error=artifacts.suggestions_error if status == 'failed' else None)
        if status != 'ready':
            return result
    data, revision, _ = _download(run_id, f'{kind}.md', token)
    content, truncated = _bounded_text(data)
    result.update(content=content, truncated=truncated)
    if revision.isdigit():
        result['revision'] = int(revision)
    return result


def answer(pid, subject, token, run_id, result_id):
    """One full answer of the report's answers attachment, for preview; nothing is written to the workspace.

    The evaluation service keeps at most 8000 characters per answer, so the result stays below about 40 KiB.
    """
    if not re.fullmatch(RESULT_PATTERN, result_id):
        raise _not_found()
    run, _ = _linked_run(pid, subject, token, run_id)
    artifacts = _artifacts(run)
    if not artifacts or artifacts.report != 'ready':
        raise WorkspaceError('评测报告尚未生成', 409, 'artifacts_not_ready')
    data, _, _ = _download(run_id, 'answers.json', token)
    try:
        answers = json.loads(data).get('answers')
    except (ValueError, AttributeError) as exc:
        raise _invalid_response() from exc
    if not isinstance(answers, list):
        raise _invalid_response()
    record = next((item for item in answers if isinstance(item, dict) and item.get('resultId') == result_id), None)
    if record is None:
        raise WorkspaceError('完整回答不存在', 404, 'answer_not_found')
    full = str(record.get('finalAnswer') or '')
    length = record.get('originalLength')
    return dict(resultId=result_id, question=str(record.get('question') or ''), fullAnswer=full,
                originalLength=length if isinstance(length, int) else len(full), truncated=record.get('truncated') is True)


def regenerate(pid, subject, token, run_id):
    _, linked = _linked_run(pid, subject, token, run_id)
    try:
        artifacts = Artifacts.model_validate(
            eval_client.call('POST', f'/runs/{run_id}/artifacts/regenerate', token, body={})).model_dump()
    except ValidationError as exc:
        raise _invalid_response() from exc
    # 让只读索引的轮询立即看到“生成中”，生成结果随后由通知写入
    package_id, owner = linked['ontology_id'], _owner_of(subject)
    if not owner:
        return artifacts
    with _index_lock(package_id, owner):
        index = _read_index(package_id, owner)
        entries = [entry for entry in index['releases'].values() if entry['run_id'] == run_id]
        for entry in entries:
            entry['artifacts'] = artifacts
        if entries:
            knowledge_hub.save(_index_path(package_id, owner), index)
    return artifacts


# ---- 评测优化：把产物拉入工作区 ----


def _suggestions_name(stem):
    # 不能落在 eval-*.md：评测优化命令与泛化校验按这个模式取最新评测报告
    return 'optimization-' + stem.removeprefix('eval-') + '.md'


def _ignored_dir(path):
    """.ontology-state 下的评测文件不进工作区 git：旧工作区的 .gitignore 可能没有排除它。"""
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / '.gitignore'
    if not ignore.is_file():
        ignore.write_text('*\n', encoding='utf-8')
    return path


def _write_tables(root, tables):
    """数据源表头写成只有表头行的 CSV，供 check_eval_data.py 对账；业务数据不离开评测服务。"""
    folder = _ignored_dir(eval_data_files.data_dir(root)) / TABLES_FOLDER
    remove_tree(folder)
    folder.mkdir()
    for name, columns in (tables if isinstance(tables, dict) else {}).items():
        header = [column for column in columns if isinstance(column, str) and TABLE_NAME.fullmatch(column)] \
            if isinstance(columns, list) else []
        if TABLE_NAME.fullmatch(name) and header:
            (folder / f'{name}.csv').write_text(','.join(header) + '\n', encoding='utf-8')


def _previous_import(root, run_id, artifacts):
    """The record of this exact artifact revision, unless its files are gone or suggestions arrived since."""
    state = root.joinpath(*STATE_DIR)
    record = knowledge_hub.load(state / 'imports.json', {}).get(run_id)
    if not record or record['revision'] != artifacts.revision:
        return None
    if artifacts.suggestions == 'ready' and not record['suggestions']:
        return None
    reports = [f'{record["stem"]}.md', f'{record["stem"]}.answers.json'] + ([record['suggestions']] if record['suggestions'] else [])
    if all((root / 'reports' / name).is_file() for name in reports) and (state / f'{record["stem"]}.json').is_file():
        return record
    return None


def _fetch_import(run_id, artifacts, token):
    """All artifacts of one consistent revision; regeneration may start between the requests."""
    names = ['report.md', 'answers.json', 'context.json'] + (['suggestions.md'] if artifacts.suggestions == 'ready' else [])
    responses = {name: _download(run_id, name, token) for name in names}
    versions = {(revision, stem) for _, revision, stem in responses.values()}
    if len(versions) != 1:
        raise WorkspaceError('评测产物正在更新，请稍后重试', 409, 'artifacts_changed')
    revision, stem = versions.pop()
    if not revision.isdigit() or not re.fullmatch(STEM_PATTERN, stem):
        raise _invalid_response()
    try:
        context = json.loads(responses['context.json'][0])
    except ValueError as exc:
        raise _invalid_response() from exc
    if not isinstance(context, dict):
        raise _invalid_response()
    return int(revision), stem, {name: value[0] for name, value in responses.items()}, context


def _store_import(root, run_id, reference, revision, stem, files, context):
    report, answers = f'{stem}.md', f'{stem}.answers.json'
    suggestions = _suggestions_name(stem) if 'suggestions.md' in files else None
    written = {report: files['report.md'], answers: files['answers.json']}
    if suggestions:
        written[suggestions] = files['suggestions.md']
    (root / 'reports').mkdir(exist_ok=True)  # 不带 parents：项目已删除时不重建工作区
    for name, data in written.items():
        (root / 'reports' / name).write_bytes(data)
    state = _ignored_dir(root.joinpath(*STATE_DIR))
    (state / f'{stem}.json').write_bytes(files['context.json'])
    _write_tables(root, context.get('tables'))
    # 只提交导入的报告，助手进行中的改动不混入这个版本点
    paths = [f'reports/{name}' for name in written]
    if _git(root, 'status', '--porcelain', '--', *paths).strip():
        _git(root, 'add', '-A', '--', *paths)
        _git(root, 'commit', '-m', f'chore(eval): 导入独立评测报告 {stem}', '--', *paths)
    record = dict(revision=revision, stem=stem, suggestions=suggestions, release_id=reference.get('release_id'),
                  version=reference.get('version'), imported_at=time.time())
    history = knowledge_hub.load(state / 'imports.json', {})
    knowledge_hub.save(state / 'imports.json', {**history, run_id: record})
    return record


def optimize(manager, pid, subject, token, run_id):
    """Bring one run's artifacts into the workspace for /ontology-evolution; only for the project's current version."""
    run, linked = _linked_run(pid, subject, token, run_id)
    base = _reference(run, 'base_ref')
    if base.get('ontology_id') != linked['ontology_id'] or base.get('release_id') != linked['release_id']:
        raise WorkspaceError(f"评测版本 {base.get('version') or '未知'} 与当前建模项目版本 {linked.get('version') or '未知'} 不一致，"
                             '请评测当前版本后再优化', 409, 'version_mismatch')
    artifacts = _artifacts(run)
    if not artifacts or artifacts.report != 'ready':
        raise WorkspaceError('评测报告尚未生成，请稍后再试', 409, 'artifacts_not_ready')
    root = manager.project_root(pid)
    with manager._project_lock(pid):
        record = _previous_import(root, run_id, artifacts)
        if record:
            # 其间可能导入过别的评测，对账表头要与本次优化的报告一致
            _write_tables(root, json.loads(root.joinpath(*STATE_DIR, f'{record["stem"]}.json').read_bytes()).get('tables'))
    if not record:
        revision, stem, files, context = _fetch_import(run_id, artifacts, token)
        with manager._project_lock(pid):
            record = _store_import(root, run_id, base, revision, stem, files, context)
    remember(pid, subject, run, base, record['stem'])
    report = f'{record["stem"]}.md'
    return {'report': report, 'suggestions': record['suggestions'], 'command': f'/ontology-evolution {report}'}


# ---- 建模回写：评测优化后发布的新版本，告诉评测服务是哪一轮建议的处理结果 ----

WRITEBACK_NOTE = 1000


def _optimization_path(pid):
    # 与项目的 Hub 关联放在一起：项目删除时随之清理
    return knowledge_hub.state_dir() / 'packages' / knowledge_hub.identifier(pid) / 'evaluation-optimization.json'


def _optimization_lock(pid):
    return knowledge_hub.lock('evaluation-optimization-' + hashlib.sha256(pid.encode()).hexdigest()[:32])


def _save_optimization(pid, value):
    path = _optimization_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_hub.save(path, value)


def remember(pid, subject, run, base, stem):
    """The run whose suggestions the project's latest evaluation optimization imported; a new one replaces it."""
    with _optimization_lock(pid):
        _save_optimization(pid, dict(run_id=run['id'] if isinstance(run.get('id'), str) else '', stem=stem,
                                     origin=run.get('origin') or 'evaluation', base_release_id=base.get('release_id'),
                                     base_version=base.get('version'), optimizer=hub_packages.scope(subject),
                                     optimized_at=time.time(), result=None))


def _update_entry(package_id, subject, run_id, optimization):
    """Show what the evaluation service recorded at once, without waiting for the next reconciliation."""
    owner = _owner_of(subject)
    if not owner:
        return
    with _index_lock(package_id, owner):
        index = _read_index(package_id, owner)
        changed = False
        for section in ORIGINS.values():
            for item in index[section].values():
                if item['run_id'] == run_id:
                    item['optimization'] = optimization
                    changed = True
        if changed:
            knowledge_hub.save(_index_path(package_id, owner), index)


def _report(run_id, token, body):
    value = eval_client.call('POST', f'/runs/{run_id}/modeling-result', token, body=body)
    try:
        return Optimization.model_validate((value or {}).get('optimization')).model_dump()
    except ValidationError as exc:
        raise _invalid_response() from exc


def settle(pid, subject, token):
    """Write back the release published after the project's latest evaluation optimization, once.

    Only the user who optimized may: the evaluation service lets only a run's owner, or for feedback runs a package
    owner, record its result. A release the evaluation service cannot be reached for stays pending and is retried;
    one it refuses is not retried. Returns the local write-back state."""
    linked = hub_packages.association(pid, subject) or {}
    with _optimization_lock(pid):
        state = knowledge_hub.load(_optimization_path(pid))
        mine = state and state.get('run_id') and state.get('optimizer') == hub_packages.scope(subject)
        if not mine or not token:
            return state
        release_id = linked.get('release_id')
        result = state.get('result') or {}
        if not release_id or release_id == state.get('base_release_id'):
            return state
        if result.get('status') in ('done', 'refused') and result.get('release_id') == release_id:
            return state
        if result.get('status') == 'done':
            return state  # 同一次优化只回写第一个新版本；之后的发布要先重新评测、再评测优化
        version = linked.get('version') or ''
        body = dict(request_id='mr-' + hashlib.sha256(f"{state['run_id']}|{release_id}".encode()).hexdigest()[:32],
                    outcome='done', release_id=release_id,
                    note=f"建模按 {state.get('base_version') or '原版本'} 的优化建议修改后，发布新版本 {version}"[:WRITEBACK_NOTE])
        try:
            optimization = _report(state['run_id'], token, body)
        except WorkspaceError as exc:
            refused = exc.status in (404, 409)
            state['result'] = dict(status='refused' if refused else 'pending', release_id=release_id, version=version,
                                   message=str(exc)[:300], at=time.time())
            _save_optimization(pid, state)
            return state
        state['result'] = dict(status='done', release_id=release_id, version=version, message='', at=time.time())
        _save_optimization(pid, state)
    _update_entry(linked['ontology_id'], subject, state['run_id'], optimization)
    return state


def after_publish(pid, subject, token, receipt):
    """Best effort right after a publication with the user's login; never fails the publication."""
    if not isinstance(receipt, dict) or receipt.get('status') != 'committed' or not eval_client.configured():
        return None
    try:
        settle(pid, subject, token)
    except (OSError, WorkspaceError):
        pass
    return writeback_view(pid, subject)


def writeback_view(pid, subject):
    """What the published versions dialog shows of the latest optimization and its write-back."""
    try:
        state = knowledge_hub.load(_optimization_path(pid))
    except (OSError, ValueError):
        return None
    if not state or not state.get('run_id'):
        return None
    result = state.get('result') or {}
    return dict(run_id=state['run_id'], origin=state.get('origin') or 'evaluation', base_release_id=state.get('base_release_id'),
                base_version=state.get('base_version'), optimized_at=state.get('optimized_at'),
                mine=state.get('optimizer') == hub_packages.scope(subject), status=result.get('status') or 'waiting',
                release_id=result.get('release_id'), version=result.get('version'), message=result.get('message') or '')


def report_failure(pid, subject, token, run_id, note):
    """The user tells the evaluation service the suggestions of this run could not be applied."""
    note = (note or '').strip()
    if not 0 < len(note) <= WRITEBACK_NOTE:
        raise WorkspaceError(f'请填写处理失败的原因（不超过 {WRITEBACK_NOTE} 字）', 422, 'note_required')
    _, linked = _linked_run(pid, subject, token, run_id)
    optimization = _report(run_id, token, dict(request_id='mr-' + uuid.uuid4().hex, outcome='failed', note=note))
    _update_entry(linked['ontology_id'], subject, run_id, optimization)
    return optimization
