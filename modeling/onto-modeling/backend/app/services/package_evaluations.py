"""已发布版本的独立评测结果（ADR-0092）。

评测轮次与产物归评测服务所有。onto 只按 Hub 包和评测服务的所有者各存一份状态索引：评测服务的完成通知写入，
对账时整份换成评测服务自己的视图。同一个包可以由多位用户评测，各人的轮次只属于本人，一个人的轮次不会点亮
另一个人的按钮。当前用户在评测服务的所有者标识从对账结果里学来，不在本地按签发方推算（两边的签发方配置可能不同）。
报告、优化建议和其中链接的完整回答，查看时都以用户自己的登录读取。

轮次有两种来源：评测轮次和由消费反馈汇成的轮次，索引按版本、来源分开记。
"""
import hashlib
import json
import logging
import re
import time
from typing import Literal
import urllib.parse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import eval_client, hub_packages, knowledge_hub
from .delegated import Delegated
from .workspace import WorkspaceError, _commit_paths

logger = logging.getLogger(__name__)

ID_PATTERN = r'^[A-Za-z0-9_-]{1,128}$'
RUN_PATTERN = r'^[a-f0-9]{32}$'
STEM_PATTERN = r'^eval-\d{8}-\d{6}-[a-f0-9]{6}$'
# 评测优化导入的评测上下文：工作区里不进 git 的状态目录
STATE_DIR = ('.ontology-state', 'eval-service')
# answers.json 中的结果 ID：报告里第 N 个用例为 case-N
RESULT_PATTERN = r'^case-[1-9][0-9]{0,5}$'
STATUS_PATTERN = r'^[a-z_]{1,32}$'
VIEW_LIMIT = 2 * 1024 * 1024
ARTIFACT_LIMITS = {'report.md': 8 * 1024 * 1024, 'suggestions.md': 256 * 1024,
                   'answers.json': 8 * 1024 * 1024, 'context.json': 2 * 1024 * 1024}


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
    """评测服务记下的「建模怎么处理了这一轮的建议」。

    状态按文本保存：评测服务新增状态时，通知不会因此被拒收。
    """
    status: str = Field(pattern=STATUS_PATTERN)
    updated_at: float | None = None
    reason: str | None = Field(default=None, max_length=2000)
    result: Outcome | None = None
    history: list[Outcome] = Field(default_factory=list, max_length=50)


class SolverSummary(_Model):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    first_failure_code: str | None = Field(default=None, max_length=120)
    first_failure_message: str | None = Field(default=None, max_length=400)


ORIGINS = {'evaluation': 'releases', 'consumer_feedback': 'feedback'}
# 评测来源的轮次再按类型分开索引：数据评测与求解验证（求解契约端到端验证）
RUN_TYPES = Literal['data', 'solver']
SECTIONS = ('releases', 'feedback', 'solver_releases')


class Entry(_Model):
    release_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(min_length=1, max_length=64)
    run_id: str = Field(pattern=RUN_PATTERN)
    status: str = Field(pattern=STATUS_PATTERN)
    # 不带 run_type 的通知来自只有数据评测的评测服务
    run_type: RUN_TYPES = 'data'
    created_at: float | None = None
    finished_at: float | None = None
    artifacts: Artifacts
    summary: Summary
    # 支持数据飞轮之前的评测服务，轮次上两项都没有
    origin: str = Field(default='evaluation', pattern=STATUS_PATTERN)
    optimization: Optimization | None = None
    solver_summary: SolverSummary | None = None


def bucket(origin, run_type='data'):
    """轮次所在的索引分区；更新版评测服务的未知来源不显示。

    评测来源再按类型分：求解验证单独一个分区。
    """
    section = ORIGINS.get(origin)
    return 'solver_releases' if section == 'releases' and run_type == 'solver' else section


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
    run_type: RUN_TYPES = 'data'


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
    # 早先的索引可能没有 feedback 或 solver_releases，按空处理
    return {'ontology_id': ontology_id, 'active': value.get('active'),
            **{section: value.get(section, {}) for section in SECTIONS}, 'reconciled_at': value.get('reconciled_at')}


def _owner_path(subject):
    return knowledge_hub.state_dir() / 'evaluation-owners' / f'{hashlib.sha256(subject.encode()).hexdigest()[:32]}.json'


def _owner_of(subject):
    """当前登录用户在评测服务的所有者标识，以评测服务最近一次对账给出的为准。"""
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
    """把一条通知落盘到所属用户的索引；重复和迟到的通知不改变任何东西。"""
    entry = {**_entry(notification), 'sequence': notification.sequence, 'received_at': time.time()}
    package_id, owner = notification.ontology_id, notification.owner
    section = bucket(notification.origin, notification.run_type)
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
    """把调用者的索引整份换成评测服务的视图，评测服务里删掉的轮次随之消失。

    委托读取（用户不在时的数据飞轮，ADR-0108）只列得到用户本人的评测轮次，消费反馈部分保留上次用用户登录对账的结果。"""
    started = time.time()
    covered = ('releases', 'solver_releases') if isinstance(token, Delegated) else SECTIONS
    # 两种来源都要：评测轮次，以及包负责人确认过的消费反馈轮次；旧版评测服务忽略 origin，只返回评测轮次
    query = urllib.parse.urlencode({'ontology_id': package_id, 'origin': 'all'})
    value = eval_client.call('GET', f'/package-evaluations?{query}', token)
    if not isinstance(value, dict) or not isinstance(value.get('releases'), list):
        raise _invalid_response()
    owner = value.get('owner')
    if not isinstance(owner, str) or not re.fullmatch(ID_PATTERN, owner):
        raise _invalid_response()
    # 只有数据评测的评测服务不返回 solver_releases
    solver = value.get('solver_releases') or []
    if not isinstance(solver, list):
        raise _invalid_response()
    try:
        fetched = [_entry(Entry.model_validate(item)) for item in value['releases']]
        fetched += [_entry(Entry.model_validate({**item, 'run_type': 'solver'})) for item in solver]
        active = Active.model_validate(value['active']).model_dump() if value.get('active') else None
    except (ValidationError, TypeError) as exc:
        raise _invalid_response() from exc
    if _owner_of(subject) != owner:
        knowledge_hub.save(_owner_path(subject), {'owner': owner})
    with _index_lock(package_id, owner):
        index = _read_index(package_id, owner)
        sections = {section: {} for section in covered}
        for entry in fetched:
            section = bucket(entry['origin'], entry['run_type'])
            if section not in sections:
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


def _solver_passed(entry):
    summary = (entry or {}).get('solver_summary') or {}
    return bool(entry) and entry.get('status') == 'completed' and summary.get('total', 0) > 0 and summary.get('failed') == 0


def verified_solver_release(pid, subject, revision):
    """这个求解契约修订最近一次求解验证通过时，返回被验证的发布。

    与求解面板同一口径：同一修订的另一个发布后来验证失败，盖过先前的通过；验证还在跑算未验证。
    只读本地索引，还没同步过来的通过仍要求确认。
    """
    linked = hub_packages.association(pid, subject) if subject else None
    if not revision or not linked:
        return None
    index = _read_index(linked['ontology_id'], _owner_of(subject))
    releases = [release_id for release_id, recorded in hub_packages.solver_revisions(pid).items() if recorded == revision]
    active = index['active'] or {}
    if active.get('run_type') == 'solver' and active.get('release_id') in releases:
        return None
    validated = [(release_id, index['solver_releases'][release_id])
                 for release_id in releases if release_id in index['solver_releases']]
    if not validated:
        return None
    release_id, entry = max(validated, key=lambda item: _moment(item[1]))
    return {'release_id': release_id, 'version': entry['version']} if _solver_passed(entry) else None


def _view(configured, linked=None, solver_revisions=None, message=None):
    linked = linked or {}
    return dict(configured=configured, reachable=None, message=message,
                ontology_id=linked.get('ontology_id'), current_release_id=linked.get('release_id'),
                current_version=linked.get('version'), active=None, releases={}, feedback={},
                solver_releases={}, solver_revisions=solver_revisions or {})


def overview(pid, subject, token, refresh=False):
    linked = hub_packages.association(pid, subject) or {}
    result = _view(eval_client.configured(), linked, hub_packages.solver_revisions(pid))
    if not result['configured']:
        return {**result, 'message': '未配置评测服务地址'}
    if not linked:
        return result
    if refresh:
        # 先补上未完成的回写（ADR-0108），随后的对账就能看到评测服务记下的处理结果；回写失败不影响查看
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
        except WorkspaceError as exc:
            # 只记状态码与错误码：消息可能带上游内容
            logger.warning('Evaluation reconciliation failed status=%s code=%s', exc.status, exc.code)
            result.update(reachable=False, message='评测服务暂不可用，显示的是最近一次同步的评测状态')

    def shown(section):
        return {release_id: {key: value for key, value in entry.items() if key not in ('sequence', 'received_at')}
                for release_id, entry in index[section].items()}
    return {**result, 'active': index['active'], **{section: shown(section) for section in SECTIONS}}


def _reference(run, key):
    value = run.get(key)
    return value if isinstance(value, dict) else {}


def _linked_run(pid, subject, token, run_id):
    """评测的是本项目 Hub 包的轮次；不是的一律按不存在处理。"""
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
    """一轮的报告或优化建议，供查看：最多 2 MiB，在行边界截断。"""
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
    """报告「完整回答」附件里的一条，供预览；不写工作区。

    评测服务每条回答最多留 8000 字符，结果约 40 KiB 以内。
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
        entries = []
        for section in SECTIONS:
            entries += [entry for entry in index[section].values() if entry['run_id'] == run_id]
        for entry in entries:
            entry['artifacts'] = artifacts
        if entries:
            knowledge_hub.save(_index_path(package_id, owner), index)
    return artifacts


# ---- 评测优化：把产物拉入工作区 ----


def _suggestions_name(stem):
    # 不能落在 eval-*.md：评测优化命令与泛化校验按这个模式取最新评测报告
    return 'optimization-' + stem.removeprefix('eval-') + '.md'


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


def _store_import(root, run_id, reference, revision, stem, files):
    report, answers = f'{stem}.md', f'{stem}.answers.json'
    suggestions = _suggestions_name(stem) if 'suggestions.md' in files else None
    written = {report: files['report.md'], answers: files['answers.json']}
    if suggestions:
        written[suggestions] = files['suggestions.md']
    (root / 'reports').mkdir(exist_ok=True)  # 不带 parents：项目已删除时不重建工作区
    for name, data in written.items():
        (root / 'reports' / name).write_bytes(data)
    state = root.joinpath(*STATE_DIR)  # .ontology-state 不进工作区 git
    state.mkdir(parents=True, exist_ok=True)
    (state / f'{stem}.json').write_bytes(files['context.json'])
    # 只提交导入的报告，助手进行中的改动不混入这个版本点
    _commit_paths(root, [f'reports/{name}' for name in written], f'feedback: 评测优化导入 {stem}')
    record = dict(revision=revision, stem=stem, suggestions=suggestions, release_id=reference.get('release_id'),
                  version=reference.get('version'), imported_at=time.time())
    history = knowledge_hub.load(state / 'imports.json', {})
    knowledge_hub.save(state / 'imports.json', {**history, run_id: record})
    return record


def optimize(manager, pid, subject, token, run_id, any_version=False, flywheel_run=None):
    """把一轮评测的产物拉进工作区交给 /ontology-evolution 的飞轮模式（ADR-0108）；只对项目当前版本。

    any_version：数据飞轮也优化较早版本的消费反馈，逐条先对照当前内容核对。flywheel_run：见 remember。"""
    run, linked = _linked_run(pid, subject, token, run_id)
    if run.get('run_type') == 'solver':
        # 求解验证的修复指引由失败码确定性生成，按指引修改求解契约后发布新版本再验证
        raise WorkspaceError('求解验证不支持评测优化：请按修复指引修改求解契约后发布新版本，再发起求解验证', 409,
                             'optimization_not_supported')
    base = _reference(run, 'base_ref')
    earlier = base.get('release_id') != linked['release_id']
    if base.get('ontology_id') != linked['ontology_id'] or (earlier and not any_version):
        raise WorkspaceError(f"评测版本 {base.get('version') or '未知'} 与当前建模项目版本 {linked.get('version') or '未知'} 不一致，"
                             '请评测当前版本后再优化', 409, 'version_mismatch')
    artifacts = _artifacts(run)
    if not artifacts or artifacts.report != 'ready':
        raise WorkspaceError('评测报告尚未生成，请稍后再试', 409, 'artifacts_not_ready')
    root = manager.project_root(pid)
    with manager._project_lock(pid):
        record = _previous_import(root, run_id, artifacts)
    if not record:
        revision, stem, files, _context = _fetch_import(run_id, artifacts, token)
        with manager._project_lock(pid):
            record = _store_import(root, run_id, base, revision, stem, files)
    # 回写以优化开始时的版本为基线：反馈针对的旧版本之后已有的版本不是这次优化的结果
    remember(pid, subject, run, linked if earlier else base, record['stem'], flywheel_run)
    report = f'{record["stem"]}.md'
    return {'report': report, 'suggestions': record['suggestions'], 'command': f'/ontology-evolution {report}'}


# ---- 建模回写：数据飞轮优化后发布的新版本，告诉评测服务是哪一轮建议的处理结果（ADR-0108） ----

WRITEBACK_NOTE = 1000
# 被新的评测优化替换时还没写回的早先结果，最多留这么多条等其优化者回来写回
EARLIER_LIMIT = 10


def _optimization_path(pid):
    # 与项目的 Hub 关联放在一起：项目删除时随之清理
    return knowledge_hub.state_dir() / 'packages' / knowledge_hub.identifier(pid) / 'evaluation-optimization.json'


def _optimization_lock(pid):
    return knowledge_hub.lock('evaluation-optimization-' + hashlib.sha256(pid.encode()).hexdigest()[:32])


def _save_optimization(pid, value):
    path = _optimization_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_hub.save(path, value)


def remember(pid, subject, run, base, stem, flywheel_run=None):
    """The run whose suggestions the project's latest evaluation optimization imported; a new one replaces it.

    One whose pinned release is not written back yet is kept aside (``earlier``) for its optimizer's next visit:
    background flywheel rounds go on while writing back waits for that user's login.
    ``flywheel_run``: the data flywheel run that revises the workspace for it. Until that revision ends
    (end_revision), releases do not count as its result: they carry half of the revision at most."""
    flywheel = dict(flywheel_run=flywheel_run, optimizing=True) if flywheel_run else {}
    with _optimization_lock(pid):
        earlier = _earlier(_loaded(pid))
        _save_optimization(pid, dict(run_id=run['id'] if isinstance(run.get('id'), str) else '', stem=stem,
                                     origin=run.get('origin') or 'evaluation', base_release_id=base.get('release_id'),
                                     base_version=base.get('version'), optimizer=hub_packages.scope(subject),
                                     optimized_at=time.time(), result=None, **flywheel,
                                     **({'earlier': earlier} if earlier else {})))


def _loaded(pid):
    try:
        return knowledge_hub.load(_optimization_path(pid)) or {}
    except (OSError, ValueError):
        return {}


def _earlier(state):
    """The results still to write back once ``state`` is replaced: its own pinned release among them."""
    earlier = list(state.get('earlier') or [])
    result = state.get('result') or {}
    if state.get('run_id') and state.get('release') and result.get('status') not in ('done', 'refused'):
        earlier.append({key: state.get(key) for key in ('run_id', 'origin', 'base_version', 'optimizer', 'release')})
    return earlier[-EARLIER_LIMIT:]


def _drop_current(pid, state):
    """Forget the latest optimization; earlier results still to write back stay."""
    if state.get('earlier'):
        _save_optimization(pid, {'earlier': state['earlier']})
    else:
        _optimization_path(pid).unlink(missing_ok=True)


def _unsettled(pid):
    """The project's latest optimization while nothing was written back for it yet; None otherwise."""
    try:
        state = knowledge_hub.load(_optimization_path(pid))
    except (OSError, ValueError):
        return None  # 读不了的记录与没有记录一样：settle 也不会据此回写
    return state if state and state.get('run_id') and not state.get('result') else None


def _belongs(state, subject, run_id, flywheel_run):
    """Whether the record is this user's optimization of this evaluation run, started by this flywheel run."""
    return (bool(run_id) and state.get('run_id') == run_id and state.get('optimizer') == hub_packages.scope(subject)
            and state.get('flywheel_run') == flywheel_run)


def forget(pid, subject, run_id, flywheel_run):
    """Drop the project's latest optimization when the flywheel run that started it stands for no release: its
    changes were taken back (stopped), it needed none (passed, already resolved) or the user ignored it.

    settle would otherwise write the next release back as this run's result, whoever made that release. A release
    already pinned (pin_release) stays: it was published before and is the result."""
    with _optimization_lock(pid):
        state = _unsettled(pid)
        if state and not state.get('release') and _belongs(state, subject, run_id, flywheel_run):
            _drop_current(pid, state)


def forget_after_rollback(pid):
    """A rollback took back every change made since the run's snapshot: drop the project's latest optimization
    unless a release already counts as its result (pin_release). Later releases, the restored content among them,
    do not."""
    with _optimization_lock(pid):
        state = _unsettled(pid)
        if state and not state.get('release'):
            _drop_current(pid, state)


def end_revision(pid, subject, run_id, flywheel_run, own_release):
    """The flywheel run that started the project's latest optimization stopped revising the workspace; from now on
    releases may count as its result.

    ``own_release``: it ended without changing the package, leaving the suggestions to a person; only a release its
    optimizer publishes then counts (they may finish them by hand), not one someone else publishes meanwhile."""
    with _optimization_lock(pid):
        state = _unsettled(pid)
        if state and _belongs(state, subject, run_id, flywheel_run):
            state['optimizing'] = False
            state['own_release'] = own_release
            if own_release and (state.get('release') or {}).get('publisher') != state['optimizer']:
                state.pop('release', None)  # 此前钉住的别人的版本（如重新检查之前）同样不算
            _save_optimization(pid, state)


def _counted(state, linked):
    """The release in ``linked`` when it counts as the optimization's result: published after the optimization
    started, and by its optimizer when the optimization itself changed nothing."""
    release_id = linked.get('release_id')
    if not release_id or release_id == state.get('base_release_id'):
        return None
    if state.get('own_release') and linked.get('publisher') != state['optimizer']:
        return None
    return dict(release_id=release_id, version=linked.get('version') or '', publisher=linked.get('publisher'))


def pin_release(pid, linked):
    """Keep the first release that counts as the result of the project's latest optimization, as it is recorded.

    settle writes that one back, not whatever is current by then: a later release or a rollback's restored
    content would otherwise take its place."""
    with _optimization_lock(pid):
        state = _unsettled(pid)
        release = state and not state.get('release') and not state.get('optimizing') and _counted(state, linked)
        if release:
            _save_optimization(pid, {**state, 'release': release})


def _update_entry(package_id, subject, run_id, optimization):
    """Show what the evaluation service recorded at once, without waiting for the next reconciliation."""
    owner = _owner_of(subject)
    if not owner:
        return
    with _index_lock(package_id, owner):
        index = _read_index(package_id, owner)
        changed = False
        for section in SECTIONS:
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


def _send(record, release, token):
    """Write ``release`` back as the result of the optimization ``record``: (local result, recorded optimization).

    The request ID is fixed per run and release, so a retry records it once. Refused (404, 409) is final;
    anything else stays pending and is retried."""
    release_id, version = release['release_id'], release['version']
    body = dict(request_id='mr-' + hashlib.sha256(f"{record['run_id']}|{release_id}".encode()).hexdigest()[:32],
                outcome='done', release_id=release_id,
                note=f"建模按 {record.get('base_version') or '原版本'} 的优化建议修改后，发布新版本 {version}"[:WRITEBACK_NOTE])
    try:
        optimization = _report(record['run_id'], token, body)
    except WorkspaceError as exc:
        refused = exc.status in (404, 409)
        return dict(status='refused' if refused else 'pending', release_id=release_id, version=version,
                    message=str(exc)[:300], at=time.time()), None
    return dict(status='done', release_id=release_id, version=version, message='', at=time.time()), optimization


def _settle_earlier(pid, subject, token):
    """Write back the results of this user's earlier optimizations a later one replaced (remember)."""
    scope = hub_packages.scope(subject)
    recorded = []
    with _optimization_lock(pid):
        state = _loaded(pid)
        earlier = state.get('earlier') or []
        if not any(item.get('optimizer') == scope for item in earlier):
            return recorded
        kept = []
        for item in earlier:
            if item.get('optimizer') != scope:
                kept.append(item)
                continue
            item['result'], optimization = _send(item, item['release'], token)
            if optimization:
                recorded.append((item['run_id'], optimization))
            elif item['result']['status'] == 'pending':
                kept.append(item)
        state['earlier'] = kept
        _save_optimization(pid, state)
    return recorded


def settle(pid, subject, token):
    """Write back the release published after the project's latest evaluation optimization, once; first those of
    this user's earlier optimizations a later one replaced before they were written back.

    Only the user who optimized may: the evaluation service lets only a run's owner, or for feedback runs a package
    owner, record its result. A release the evaluation service cannot be reached for stays pending and is retried;
    one it refuses is not retried. Returns the local write-back state."""
    linked = hub_packages.association(pid, subject) or {}
    if linked and token and not isinstance(token, Delegated):
        for run_id, optimization in _settle_earlier(pid, subject, token):
            _update_entry(linked['ontology_id'], subject, run_id, optimization)
    with _optimization_lock(pid):
        state = knowledge_hub.load(_optimization_path(pid))
        mine = state and state.get('run_id') and state.get('optimizer') == hub_packages.scope(subject)
        # 数据飞轮的后台委托不能代用户回写：留到用户本人带登录访问时
        own_login = bool(token) and not isinstance(token, Delegated)
        if not mine or not own_login or state.get('optimizing'):
            return state  # 飞轮还在修订时，版本至多含一半修订，不是处理结果
        result = state.get('result') or {}
        release = state.get('release')  # 发布时记下的版本（pin_release）
        if not release and result.get('status') == 'pending':
            # 本功能之前开始的重试：沿用当时回写的版本
            release = dict(release_id=result['release_id'], version=result.get('version') or '')
        if not release and not linked.get('publisher'):
            release = _counted(state, linked)  # 本功能之前记下的关联：按当前版本判断
        if not release:
            return state
        release_id = release['release_id']
        if result.get('status') in ('done', 'refused') and result.get('release_id') == release_id:
            return state
        if result.get('status') == 'done':
            return state  # 同一次优化只回写第一个新版本；之后的发布要先重新评测、再评测优化
        state['release'] = release
        state['result'], optimization = _send(state, release, token)
        _save_optimization(pid, state)
        if not optimization:
            return state
    _update_entry(linked['ontology_id'], subject, state['run_id'], optimization)
    return state


def writeback_view(pid, subject):
    """项目最近一次评测优化及其回写的状态（数据飞轮与测试查看用；ADR-0101 后界面不再展示）。"""
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


def report_resolved(pid, subject, token, run_id, release_id, version):
    """Tell the evaluation service the latest version no longer has this run's problems; nothing was published.

    The request ID is fixed per run and checked release, so a retry records the result once. When the project's
    latest optimization is still this run, it then has its answer: a later publication is not written back as its
    result. (The flywheel drops that record as soon as the run concludes as already resolved, optimize.check.)"""
    _, linked = _linked_run(pid, subject, token, run_id)
    note = f'建模核对后，最新版本 {version or release_id} 已不存在这些问题，未修改知识包'[:WRITEBACK_NOTE]
    body = dict(request_id='mr-' + hashlib.sha256(f'{run_id}|resolved|{release_id}'.encode()).hexdigest()[:32],
                outcome='resolved', release_id=release_id, note=note)
    optimization = _report(run_id, token, body)
    with _optimization_lock(pid):
        state = knowledge_hub.load(_optimization_path(pid))
        if state and state.get('run_id') == run_id:
            state['result'] = dict(status='done', outcome='resolved', release_id=release_id, version=version,
                                   message='', at=time.time())
            _save_optimization(pid, state)
    _update_entry(linked['ontology_id'], subject, run_id, optimization)
    return optimization
