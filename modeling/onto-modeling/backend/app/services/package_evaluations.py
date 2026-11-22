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
from .workspace import WorkspaceError

logger = logging.getLogger(__name__)

ID_PATTERN = r'^[A-Za-z0-9_-]{1,128}$'
RUN_PATTERN = r'^[a-f0-9]{32}$'
STEM_PATTERN = r'^eval-\d{8}-\d{6}-[a-f0-9]{6}$'
# answers.json 中的结果 ID：报告里第 N 个用例为 case-N
RESULT_PATTERN = r'^case-[1-9][0-9]{0,5}$'
STATUS_PATTERN = r'^[a-z_]{1,32}$'
VIEW_LIMIT = 2 * 1024 * 1024
ARTIFACT_LIMITS = {'report.md': 8 * 1024 * 1024, 'suggestions.md': 256 * 1024,
                   'answers.json': 8 * 1024 * 1024}


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
    """把调用者的索引整份换成评测服务的视图，评测服务里删掉的轮次随之消失。"""
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
