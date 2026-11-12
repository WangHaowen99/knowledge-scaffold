"""快照入库（`design/evidence-collection.md` §3 末尾的校验、§6「快照入库」；ADR-0082）。

快照包是网关给的 tar，内容来自目标程序，按不可信数据处理：只认清单里的几种文件、逐个核哈希、记录逐行校验；
任何一项不过整个任务失败，工作区里不留半份快照。通过后：

1. 文本文件过一遍密钥扫描（规则复用遥测脱敏和包检查的两组），命中的值换成「«已遮蔽»」，条数记进清单 `masked_hits`；
2. `blobs/` 收进 blob 仓（工作区外）；
3. 其余文件经写入事务提交到 `resource/systems/<源>/<快照>/`，清单补 `snapshot_id`、`previous`、`initiated_by`、`cred_ref`；
4. 建索引。

连接型（F16）：`sample`、`profile` 记录在密钥扫描之后再扫一遍个人信息（`pii`）；`discover` 只收清单，不提交快照（`inventory_only`）。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import tarfile
import time
import uuid
from pathlib import Path

from ..governance import telemetry
from ..services.packs import checks
from . import RECORD_TYPES, SNAPSHOT_FORMAT, spec, storage

AUTHOR = "多源连接器"
GIT_MB_ENV = "ONTO_WEB_EVIDENCE_SNAPSHOT_GIT_MB"
MASK = "«已遮蔽»"
SYSTEMS_DIR = "resource/systems"
RECORD_KEYS = ("id", "type", "parent", "name", "label", "attrs", "evidence", "hash")
# 提供方类型 → 锚点 scheme（REA 出的是 bin:）
SCHEMES = {"rea": "bin", "db": "db", "code": "code", "ui": "ui", "api": "api", "cli": "cli", "log": "log"}
_RECORD_FILE = re.compile(r"^records/([a-z]+)\.jsonl$")
_BLOB_FILE = re.compile(r"^blobs/([0-9a-f]{64})$")
_TEXT_FILES = ("manifest.json", "inventory.json", "summary.md")
_COPY_BYTES = 1024 * 1024
_PEM_END = "-----END "


class IngestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid(message: str) -> IngestError:
    return IngestError("bundle_invalid", f"快照包校验没通过：{message}")


def git_limit_bytes() -> int:
    from ..governance import config_registry

    return int(config_registry.env_value(GIT_MB_ENV)) * 1024 * 1024


def new_snapshot_id(root: Path, now: float | None = None) -> str:
    """`YYYYMMDD-HHMMSS-xxxx`（UTC）。按字典序就是先后（检索、`evidence.py` 都这样取最新）：同一秒里已经有快照
    （任何源）时后缀在最大的那个上加一，项目内也就不会重号。调用方持项目锁。"""
    from .index import workspace_snapshots

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    latest = max(workspace_snapshots(root), default="")
    if latest[:15] >= stamp:  # 同一秒（或时钟回拨）：接着最大的那个编
        try:
            return f"{latest[:15]}-{int(latest[16:], 16) + 1:04x}"
        except ValueError:
            stamp = latest[:15]
    return f"{stamp}-{secrets.randbelow(0x8000):04x}"


def scheme_for(kind: str) -> str:
    return SCHEMES.get(kind or "", "bin")


# ---------------------------------------------------------------------------
# 密钥遮蔽
# ---------------------------------------------------------------------------


def mask_text(text: str) -> tuple[str, int]:
    """遥测脱敏的规则保留键名、只换值；包检查的规则（私钥、云厂商密钥）整段换掉，私钥一直换到 END 行。"""
    hits = 0

    def keep_prefix(match: re.Match) -> str:
        nonlocal hits
        hits += 1
        return (match.group(1) if match.re.groups else "") + MASK

    for pattern in telemetry._SECRET_PATTERNS:
        text = pattern.sub(keep_prefix, text)
    for pattern in checks._SECRET_PATTERNS:
        out, position = [], 0
        for match in pattern.finditer(text):
            if match.start() < position:
                continue
            end = match.end()
            if match.group(0).startswith("-----BEGIN"):
                closing = text.find(_PEM_END, end)
                if closing >= 0:
                    tail = text.find("-----", closing + len(_PEM_END))
                    end = tail + 5 if tail >= 0 else len(text)
            out.append(text[position:match.start()] + MASK)
            position = end
            hits += 1
        text = "".join(out) + text[position:]
    return text, hits


def _mask_value(value, counter: list[int]):
    if isinstance(value, str):
        masked, hits = mask_text(value)
        counter[0] += hits
        return masked
    if isinstance(value, list):
        return [_mask_value(item, counter) for item in value]
    if isinstance(value, dict):
        return {key: _mask_value(item, counter) for key, item in value.items()}
    return value


def _mask_record(record: dict) -> tuple[dict, int]:
    """只遮蔽 `label` 和 `attrs`：锚点、名字是记录的身份，改了就对不上了；遮蔽后重算 `hash`。"""
    counter = [0]
    masked = {**record, "label": _mask_value(record.get("label"), counter),
              "attrs": _mask_value(record["attrs"], counter)}
    if counter[0]:
        masked["hash"] = spec.record_hash(masked)
        return masked, counter[0]
    return record, 0


# ---------------------------------------------------------------------------
# 解包与校验
# ---------------------------------------------------------------------------


def _unpack(bundle: Path, work: Path, git_limit: int) -> tuple[dict[str, bytes], dict[str, Path]]:
    """流式读 tar：文本部分进内存（合计受 git 上限约束），blob 边写边算哈希。"""
    texts: dict[str, bytes] = {}
    blobs: dict[str, Path] = {}
    git_bytes = 0
    # 不压缩的 tar：条目合计不会超过包本身，超了就是展开型的条目
    unpacked, budget = 0, Path(bundle).stat().st_size
    try:
        with tarfile.open(bundle, mode="r|") as archive:  # NOSONAR(S5042) 逐条校验类型和名字，合计不超过包本身
            for member in archive:
                name = member.name.removeprefix("./")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise _invalid(f"不允许的条目类型 {name[:80]}")
                unpacked += member.size
                if unpacked > budget:
                    raise _invalid(f"条目大小合计超过包本身 {name[:80]}")
                if name in texts or name in blobs:
                    raise _invalid(f"重复的文件 {name[:80]}")
                handle = archive.extractfile(member)
                blob = _BLOB_FILE.match(name)
                if blob:
                    digest, target = hashlib.sha256(), work / f"blob-{len(blobs)}"
                    with target.open("wb") as out:
                        piece = handle.read(_COPY_BYTES)
                        while piece:
                            digest.update(piece)
                            out.write(piece)
                            piece = handle.read(_COPY_BYTES)
                    if digest.hexdigest() != blob.group(1):
                        raise _invalid(f"原始结果 {blob.group(1)[:12]} 的哈希不对")
                    blobs[name] = target
                    continue
                if name not in _TEXT_FILES and not _RECORD_FILE.match(name):
                    raise _invalid(f"不认识的文件 {name[:80]}")
                git_bytes += member.size
                if git_bytes > git_limit:
                    raise IngestError("snapshot_too_large",
                                      f"快照进 git 的部分超过 {git_limit // (1024 * 1024)} MB")
                texts[name] = handle.read()
    except tarfile.TarError as exc:
        raise _invalid(f"不是有效的 tar（{type(exc).__name__}）") from None
    return texts, blobs


def _manifest(texts: dict[str, bytes], blobs: dict[str, Path], source_id: str, job_id: str) -> dict:
    try:
        manifest = json.loads(texts["manifest.json"].decode("utf-8"))
    except KeyError:
        raise _invalid("缺 manifest.json") from None
    except ValueError:
        raise _invalid("manifest.json 不是 JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != SNAPSHOT_FORMAT:
        raise _invalid("format 不对")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    if source.get("id") != source_id:
        raise _invalid("数据源不符")
    if manifest.get("job_id") not in (None, job_id):
        raise _invalid("任务号不符")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise _invalid("缺 files")
    for name in texts:
        if name != "manifest.json" and name not in files:
            raise _invalid(f"{name} 没有登记哈希")
    for name, digest in files.items():
        if name not in texts:
            raise _invalid(f"缺登记的文件 {str(name)[:80]}")
        if hashlib.sha256(texts[name]).hexdigest() != digest:
            raise _invalid(f"{name} 的哈希对不上")
    listed = manifest.get("blobs") or []
    if not isinstance(listed, list) or not all(isinstance(item, dict) for item in listed):
        raise _invalid("blobs 不是列表")
    if {f"blobs/{item.get('sha256')}" for item in listed} != set(blobs):
        raise _invalid("原始结果和清单不一致")
    return manifest


def _well_typed(record: dict, kind: str, prefix: str) -> bool:
    """记录的类型、锚点前缀和各字段的类型都对。"""
    anchor = record["id"]
    if record["type"] != kind or not isinstance(anchor, str) or not anchor.startswith(prefix):
        return False
    if not isinstance(record["name"], str) or not isinstance(record["attrs"], dict):
        return False
    return isinstance(record["evidence"], dict) and isinstance(record["hash"], str)


def _records(texts: dict[str, bytes], prefix: str) -> dict[str, list[dict]]:
    seen: set[str] = set()
    out: dict[str, list[dict]] = {}
    for name, raw in texts.items():
        matched = _RECORD_FILE.match(name)
        if not matched:
            continue
        kind = matched.group(1)
        if kind not in RECORD_TYPES:
            raise _invalid(f"记录类型 {kind} 不认识")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise _invalid(f"{name} 不是 UTF-8") from None
        rows = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise _invalid(f"{name} 第 {number} 行不是 JSON") from None
            if not isinstance(record, dict) or any(key not in record for key in RECORD_KEYS):
                raise _invalid(f"{name} 第 {number} 行字段不齐")
            anchor = record["id"]
            if not _well_typed(record, kind, prefix):
                raise _invalid(f"{name} 第 {number} 行字段不对")
            if anchor in seen:
                raise _invalid(f"锚点重复：{anchor[:120]}")
            seen.add(anchor)
            rows.append(record)
        out[kind] = rows
    return out


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------


def snapshot_dir(source_id: str, snapshot_id: str) -> str:
    return f"{SYSTEMS_DIR}/{source_id}/{snapshot_id}"


ACCESS_LOG_MAX = 1000
# 内核再扫一遍个人信息的记录类型（数据库的样本、画像）
PII_TYPES = ("sample", "profile")


def access_log(manifest: dict) -> list[dict]:
    """清单的 `access_log`（spec §8）：只留规定的字段，最多 1000 条。"""
    out = []
    for item in manifest.get("access_log") or [] if isinstance(manifest.get("access_log"), list) else []:
        if not isinstance(item, dict):
            continue
        entry = {"statement_digest": str(item.get("statement_digest") or "")[:80]}
        for key in ("rows", "bytes", "duration_ms"):
            value = item.get(key)
            entry[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        if isinstance(item.get("at"), str):
            entry["at"] = item["at"][:40]
        out.append(entry)
        if len(out) >= ACCESS_LOG_MAX:
            break
    return out


def _open_bundle(bundle: Path, work: Path, source_id: str,
                 job_id: str) -> tuple[dict[str, bytes], dict[str, Path], dict]:
    texts, blobs = _unpack(bundle, work, git_limit_bytes())
    return texts, blobs, _manifest(texts, blobs, source_id, job_id)


def _inventory(texts: dict[str, bytes]) -> tuple[dict | None, int]:
    if "inventory.json" not in texts:
        return None, 0
    try:
        inventory = json.loads(texts["inventory.json"].decode("utf-8"))
    except ValueError:
        raise _invalid("inventory.json 不是 JSON") from None
    counter = [0]
    inventory = _mask_value(inventory, counter)
    return inventory, counter[0]


def inventory_only(project_id: str, registration: dict, job: dict, bundle: Path) -> dict:
    """`discover`：快照包只有清单，不提交快照，存到 `inventories/<项目>/<源>.json`；返回 {manifest, items, skipped}。"""
    work = storage.tmp_dir() / f"{job['id']}-{uuid.uuid4().hex[:6]}"
    work.mkdir(parents=True)
    try:
        texts, _blobs, manifest = _open_bundle(bundle, work, registration["id"], job["id"])
        inventory, _hits = _inventory(texts)
        if not isinstance(inventory, dict):
            raise _invalid("缺 inventory.json")
        items = [item for item in inventory.get("items") or [] if isinstance(item, dict)]
        skipped = [item for item in inventory.get("skipped") or [] if isinstance(item, dict)]
        storage.write_json(storage.inventory_path(project_id, registration["id"]), {
            "job_id": job["id"], "at": time.time(), "scope": list(job.get("scope") or []),
            "items": items, "skipped": skipped})
        return {"manifest": manifest, "items": len(items), "skipped": len(skipped)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ingest(manager, project_id: str, registration: dict, job: dict, bundle: Path) -> dict:
    """校验、遮蔽、收 blob、提交、建索引；返回 {snapshot_id, commit, manifest}。不通过抛 IngestError。

    `sample` 入库成补充快照：清单 `operation=sample`、`complete=false`、`scope=[selector]`（不管网关怎么写）。"""
    from ..kernel.writepath import ChangeTx, Origin
    from . import index, pii, query

    source_id = registration["id"]
    prefix = f"{scheme_for(registration.get('kind', ''))}:{source_id}/"
    work = storage.tmp_dir() / f"{job['id']}-{uuid.uuid4().hex[:6]}"
    work.mkdir(parents=True)
    try:
        texts, blobs, manifest = _open_bundle(bundle, work, source_id, job["id"])
        records = _records(texts, prefix)
        manifest["operation"] = job.get("operation") or "snapshot"
        if manifest["operation"] == "sample":
            manifest.update(complete=False, scope=[job["selector"]] if job.get("selector") else [])
        if "access_log" in manifest:
            manifest["access_log"] = access_log(manifest)

        masked_hits = 0
        files: dict[str, bytes] = {}
        for kind, rows in records.items():
            lines = []
            for record in rows:
                masked, hits = _mask_record(record)
                if kind in PII_TYPES:
                    masked, more = pii.mask_record(masked)
                    hits += more
                masked_hits += hits
                lines.append(json.dumps(masked, ensure_ascii=False))
            files[f"records/{kind}.jsonl"] = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        inventory, hits = _inventory(texts)
        if inventory is not None:
            masked_hits += hits
            files["inventory.json"] = json.dumps(inventory, ensure_ascii=False).encode("utf-8")
        if "summary.md" in texts:
            summary, hits = mask_text(texts["summary.md"].decode("utf-8", errors="replace"))
            masked_hits += hits
            files["summary.md"] = summary.encode("utf-8")

        counter = [0]
        manifest["limitations"] = _mask_value(manifest.get("limitations") or [], counter)
        masked_hits += counter[0]
        if sum(len(data) for data in files.values()) > git_limit_bytes():
            raise IngestError("snapshot_too_large", f"快照进 git 的部分超过 {git_limit_bytes() // (1024 * 1024)} MB")
        for name, path in blobs.items():
            storage.adopt_blob(path, name.removeprefix("blobs/"))

        # 快照 ID 和 previous 在项目锁里定：同一秒入库的两份也按先后排
        with ChangeTx(manager, project_id, Origin("plugin", AUTHOR), summary=f"采集 {source_id} 快照") as tx:
            root = tx.root
            snapshot_id = new_snapshot_id(root)
            manifest.update({
                "snapshot_id": snapshot_id,
                "previous": query.latest_snapshot(root, source_id, complete_only=True),
                "initiated_by": job.get("initiated_by") or "",
                "cred_ref": registration.get("cred_ref"),
                "masked_hits": masked_hits,
                "records": {kind: len(rows) for kind, rows in records.items()},
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
            })
            files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
            base = snapshot_dir(source_id, snapshot_id)
            for name in sorted(files):
                tx.write(f"{base}/{name}", files[name])
            result = tx.commit(f"evidence: 采集 {source_id} 快照 {snapshot_id}")
        try:
            index.add_snapshot(project_id, root, source_id, snapshot_id)
        except Exception:  # noqa: BLE001 - 索引能从工作区重建，读的时候会补
            import logging

            logging.getLogger(__name__).exception("快照建索引失败（%s）", snapshot_id)
        return {"snapshot_id": snapshot_id, "commit": result.commit, "manifest": manifest}
    finally:
        shutil.rmtree(work, ignore_errors=True)
