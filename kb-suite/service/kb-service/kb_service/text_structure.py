"""Reading helpers over a document's L1 markdown: windows with anchors, sections, outline, line grep.

Only the standard library and ``chunking`` are used, so the knowledge-backend
adapters (WeKnora, DA pipeline) install kb-service without dependencies and
reuse these for the provider contract's ``content.structure`` and ``grep``.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import contextlib
import functools
import json
import queue
import re
import struct
import subprocess  # noqa: S404 - grep runs untrusted regexes in a killable worker
import sys
import threading
import time

from .chunking import parse_blocks

MAX_PATTERN = 500
OUTLINE_LIMIT = 40


def outline(markdown: str) -> list[str]:
    heads = []
    for block in parse_blocks(markdown):
        if block.kind == "heading":
            heads.append("  " * (block.level - 1) + markdown[block.start : block.end].lstrip("#").strip())
            if len(heads) >= OUTLINE_LIMIT:
                break
    return heads


def section(markdown: str, heading: str) -> str | None:
    blocks = parse_blocks(markdown)
    target = heading.strip().lstrip("#").strip()
    for i, block in enumerate(blocks):
        if block.kind != "heading":
            continue
        title = markdown[block.start : block.end].lstrip("#").strip()
        if title == target or target in title:
            end = len(markdown)
            for later in blocks[i + 1 :]:
                if later.kind == "heading" and later.level <= block.level:
                    end = later.start
                    break
            return markdown[block.start : end]
    return None


def anchors(markdown: str, doc_id: str, start: int, end: int) -> list[dict]:
    """Anchors ``{docId}:{type}:{index}`` of the blocks overlapping [start, end)."""

    out = []
    for block in parse_blocks(markdown, doc_id):
        if block.end <= start or block.start >= end:
            continue
        out.append(
            {
                "id": block.anchor_id,
                "type": block.anchor_id.split(":")[-2] if block.anchor_id else block.kind,
                "line_start": markdown.count("\n", 0, block.start) + 1,
                "preview": markdown[block.start : block.end][:80],
                "section_title": block.header_path[-1] if block.header_path else None,
            }
        )
    return out


def window(markdown: str, doc_id: str, *, offset: int = 0, limit: int = 30000, heading: str | None = None) -> dict | None:
    """A readable piece of the document: one section (``heading``) or ``limit`` characters from ``offset``.

    A window ends on a line break when one is in its second half. ``limit=0``
    returns only the outline and the size. ``None`` when ``heading`` is not found.
    """

    total = len(markdown)
    base = {"total_chars": total, "outline": outline(markdown)}
    if heading:
        found = section(markdown, heading)
        if found is None:
            return None
        start = markdown.find(found)
        return {**base, "content": found, "offset": start, "has_more": False,
                "anchors": anchors(markdown, doc_id, start, start + len(found))}
    offset = max(0, offset)
    if limit <= 0:
        return {**base, "content": "", "offset": offset, "has_more": offset < total, "anchors": []}
    end = min(total, offset + limit)
    if end < total:
        cut = markdown.rfind("\n", offset, end)
        if cut > offset + limit // 2:
            end = cut + 1
    out = {**base, "content": markdown[offset:end], "offset": offset, "has_more": end < total,
           "anchors": anchors(markdown, doc_id, offset, end)}
    if end < total:
        out["next_offset"] = end
    return out


# ---- line grep --------------------------------------------------------------
#
# The pattern is untrusted input. CPython's ``re`` backtracks, keeps the GIL
# while matching and cannot be interrupted, so one pattern such as ``(a+)+$``
# used to pin a CPU and stall the whole service (even ``a+$`` over a 20k-char
# line takes seconds). Two layers keep a pattern from taking a service down:
#
# 1. ``compile_pattern`` rejects patterns whose backtracking grows
#    exponentially: a repeated part that can split the same text in several
#    ways (``(a+)+``, ``(a|ab)*``), a repeated part that can match nothing, and
#    backreferences.
# 2. ``grep_lines`` matches any pattern that can still backtrack (variable
#    quantifiers, overlapping alternatives) in a separate worker process with a
#    hard deadline; on timeout the worker is killed and ``PatternTimeout`` is
#    raised. Words, character classes and short fixed counts cannot backtrack
#    and match in-process.

GREP_TIMEOUT_S = 3.0  # one document's scan in a worker process
GREP_BUDGET_S = 20.0  # worker time one pattern may use per minute
GREP_WORKERS = 2
GREP_QUEUE_TIMEOUT_S = 10.0  # longest wait for a free worker process
GREP_THREADS = 8  # threads that run scans (see grep_executor)
LINE_PREVIEW = 500
_FAST_FIXED_MAX = 20  # ``\d{4}``-style fixed counts up to this match in-process

try:  # internal to ``re``; without it every pattern goes to a worker process
    from re import _constants as _sre
    from re import _parser as _sre_parser
except ImportError:  # pragma: no cover - future Python
    _sre = _sre_parser = None


class PatternError(ValueError):
    code = "bad_pattern"


class PatternTimeout(PatternError):
    code = "pattern_timeout"


_NESTED = (
    "正则过于复杂：重复的部分里又有可互相重叠的重复（如 (a+)+、(\\w+\\s?)*），"
    "匹配时间会随文字长度成倍增长。请去掉嵌套的量词，或改用普通文字查找"
)
_OVERLAP = "正则过于复杂：重复的部分里有能匹配同样文字的分支（如 (a|ab)+），匹配时间会随文字长度成倍增长，请改写"
_EMPTY_LOOP = "正则过于复杂：重复的部分可以什么都不匹配（如 (a?)+、(a*)*），请改写"
_BACKREF = "不支持反向引用（如 \\1、(?P=name)），请改用普通正则或文字查找"
_TOO_DEEP = "正则嵌套层数过多，请简化"
_PAUSED = "该正则最近执行超时或耗时过长，1 分钟内暂停使用，请简化正则或改用普通文字查找"
_BUSY = "同时进行的正则查找太多，排队超时，请稍后重试"
_UNAVAILABLE = "当前环境无法安全地执行带量词的正则，请改用普通文字查找"


def compile_pattern(pattern: str) -> tuple[re.Pattern, str]:
    """Case-insensitive regex, or the literal text when the pattern is not a valid regex.

    Raises PatternError when the pattern is empty, too long, or its
    backtracking can explode (see the notes above).
    """

    if not pattern:
        raise PatternError("pattern 不能为空")
    if len(pattern) > MAX_PATTERN:
        raise PatternError(f"pattern 超过 {MAX_PATTERN} 字")
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except (re.error, RecursionError, OverflowError):
        return re.compile(re.escape(pattern), re.IGNORECASE), "literal"
    risk = _analyze(regex.pattern, regex.flags)[0]
    if risk:
        raise PatternError(risk)
    return regex, "regex"


def grep_lines(text: str, regex: re.Pattern, max_lines: int) -> tuple[int, list[dict]]:
    """(number of matching lines, the first ``max_lines`` of them as {line_number, text}).

    Raises PatternTimeout when a backtracking pattern runs past the deadline.
    """

    if not _analyze(regex.pattern, regex.flags)[1]:
        return _scan(text, regex, max_lines)
    return _pool().grep(regex, text, max_lines)


def _scan(text: str, regex: re.Pattern, max_lines: int) -> tuple[int, list[dict]]:
    total, lines = 0, []
    for number, line in enumerate(text.split("\n"), start=1):
        if regex.search(line):
            total += 1
            if len(lines) < max_lines:
                lines.append({"line_number": number, "text": line[:LINE_PREVIEW]})
    return total, lines


# ---- pattern analysis ---------------------------------------------------------


@functools.lru_cache(maxsize=256)
def _analyze(pattern: str, flags: int) -> tuple[str | None, bool]:
    """(reason to reject, needs a worker process)."""

    if _sre_parser is None:
        return None, True
    try:
        tree = list(_sre_parser.parse(pattern, flags))
        shape = _Shape(tree, bool(flags & re.IGNORECASE))
        return shape.risk(tree), shape.needs_worker(tree)
    except RecursionError:
        return _TOO_DEEP, True


if _sre is not None:
    _ATOMIC = getattr(_sre, "ATOMIC_GROUP", None)
    _POSSESSIVE = getattr(_sre, "POSSESSIVE_REPEAT", None)
    _REPEATS = {op for op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT, _POSSESSIVE) if op is not None}
    _CHAR_OPS = {_sre.LITERAL, _sre.NOT_LITERAL, _sre.ANY, _sre.IN}
    _CATEGORIES = {
        _sre.CATEGORY_DIGIT: str.isdecimal,
        _sre.CATEGORY_NOT_DIGIT: lambda c: not c.isdecimal(),
        _sre.CATEGORY_SPACE: str.isspace,
        _sre.CATEGORY_NOT_SPACE: lambda c: not c.isspace(),
        _sre.CATEGORY_WORD: lambda c: c.isalnum() or c == "_",
        _sre.CATEGORY_NOT_WORD: lambda c: not (c.isalnum() or c == "_"),
        _sre.CATEGORY_LINEBREAK: lambda c: c == "\n",
        _sre.CATEGORY_NOT_LINEBREAK: lambda c: c != "\n",
    }

# Characters standing for the usual classes. The pattern's own literals and
# range ends are added, so two character sets overlap when a probe is in both.
_PROBES = "aZ09_ \t-.,:;/!?#中é　\x00"


def _children(op, av) -> list:
    if op is _sre.SUBPATTERN:
        return [av[3]]
    if op in _REPEATS:
        return [av[2]]
    if op is _sre.BRANCH:
        return list(av[1])
    if op is _ATOMIC:
        return [av]
    if op in (_sre.ASSERT, _sre.ASSERT_NOT):
        return [av[1]]
    if op is _sre.GROUPREF_EXISTS:
        return [av[1]] + ([av[2]] if av[2] else [])
    return []


def _overlapping(sets: list[frozenset]) -> bool:
    return any(sets[i] & sets[j] for i in range(len(sets)) for j in range(i + 1, len(sets)))


class _Shape:
    """First-character sets over a parsed pattern, enough to spot ambiguous loops."""

    def __init__(self, tree: list, ignorecase: bool) -> None:
        self.icase = ignorecase
        chars = set(_PROBES)
        self._collect(tree, chars)
        if ignorecase:
            for c in list(chars):
                chars.update(v for v in (c.lower(), c.upper(), c.casefold()) if len(v) == 1)
        self.alphabet = frozenset(chars)

    def _collect(self, seq, out: set) -> None:
        for op, av in seq:
            if op in (_sre.LITERAL, _sre.NOT_LITERAL):
                out.add(chr(av))
            elif op is _sre.IN:
                for item_op, item_av in av:
                    if item_op is _sre.LITERAL:
                        out.add(chr(item_av))
                    elif item_op is _sre.RANGE:
                        out.update((chr(item_av[0]), chr(item_av[1])))
            else:
                for sub in _children(op, av):
                    self._collect(sub, out)

    def _same(self, ch: str, literal: str) -> bool:
        return ch == literal or (self.icase and ch.casefold() == literal.casefold())

    def _in_set(self, items, ch: str) -> bool:
        negate, hit = False, False
        variants = {ch, ch.lower(), ch.upper(), ch.casefold()} if self.icase else {ch}
        for item_op, item_av in items:
            if item_op is _sre.NEGATE:
                negate = True
            elif item_op is _sre.LITERAL:
                hit = hit or self._same(ch, chr(item_av))
            elif item_op is _sre.RANGE:
                hit = hit or any(len(v) == 1 and item_av[0] <= ord(v) <= item_av[1] for v in variants)
            elif item_op is _sre.CATEGORY:
                hit = hit or _CATEGORIES.get(item_av, lambda c: True)(ch)
            else:
                hit = True
        return hit != negate

    def _matches(self, op, av, ch: str) -> bool:
        if op is _sre.LITERAL:
            return self._same(ch, chr(av))
        if op is _sre.NOT_LITERAL:
            return not self._same(ch, chr(av))
        if op is _sre.IN:
            return self._in_set(av, ch)
        return True  # ANY

    def first(self, seq) -> tuple[frozenset, bool]:
        """(characters a match of ``seq`` can start with, whether ``seq`` can match empty text)."""

        out: set = set()
        for op, av in seq:
            chars, nullable = self._first_item(op, av)
            out |= chars
            if not nullable:
                return frozenset(out), False
        return frozenset(out), True

    def _first_item(self, op, av) -> tuple[frozenset, bool]:
        if op in _CHAR_OPS:
            return frozenset(c for c in self.alphabet if self._matches(op, av, c)), False
        if op in (_sre.AT, _sre.ASSERT, _sre.ASSERT_NOT):
            return frozenset(), True
        if op in _REPEATS:
            low, high, body = av
            if high == 0:
                return frozenset(), True
            chars, nullable = self.first(body)
            return chars, nullable or low == 0
        if op is _sre.BRANCH:
            chars, nullable = set(), False
            for alt in av[1]:
                alt_chars, alt_nullable = self.first(alt)
                chars |= alt_chars
                nullable = nullable or alt_nullable
            return frozenset(chars), nullable
        if op is _sre.SUBPATTERN or op is _ATOMIC:
            return self.first(_children(op, av)[0])
        return self.alphabet, True  # backreferences: anything

    def ambiguous(self, alternatives, after: frozenset) -> bool:
        """Two alternatives can match the same text (one character of lookahead).

        ``re`` factors common prefixes out, so ``(a|a)`` arrives as ``a(?:|)``:
        two alternatives that both match empty text count as overlapping too.
        """

        looks, empty = [], 0
        for alt in alternatives:
            chars, nullable = self.first(alt)
            looks.append(chars | after if nullable else chars)
            empty += nullable
        return empty > 1 or _overlapping(looks)

    def risk(self, seq, follow: frozenset = frozenset(), in_loop: bool = False) -> str | None:
        """Why the pattern can backtrack exponentially, or None.

        ``follow`` holds the characters that can come right after ``seq``
        inside the innermost enclosing loop, including that loop's next round.
        """

        items = list(seq)
        for i, (op, av) in enumerate(items):
            rest, rest_nullable = self.first(items[i + 1 :])
            after = rest | follow if rest_nullable else rest
            found = self._item_risk(op, av, after, in_loop)
            if found:
                return found
        return None

    def _item_risk(self, op, av, after: frozenset, in_loop: bool) -> str | None:
        if op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
            return _BACKREF
        if op is _sre.SUBPATTERN:
            return self.risk(av[3], after, in_loop)
        if op is _ATOMIC:
            return self.risk(av)  # no backtracking into an atomic group
        if op in (_sre.ASSERT, _sre.ASSERT_NOT):
            return self.risk(av[1])
        if op is _sre.BRANCH:
            if in_loop and self.ambiguous(av[1], after):
                return _OVERLAP
            return next((r for r in (self.risk(alt, after, in_loop) for alt in av[1]) if r), None)
        if op in _REPEATS:
            low, high, body = av
            if op is _POSSESSIVE:
                return self.risk(body)
            if high <= 1:
                return self.risk(body, after, in_loop)
            chars, nullable = self.first(body)
            if nullable:
                return _EMPTY_LOOP
            if in_loop and chars & after:
                return _NESTED
            return self.risk(body, chars | (after if in_loop else frozenset()), True)
        return None

    def needs_worker(self, seq) -> bool:
        """The pattern can backtrack at all (more than linear work per start position)."""

        for op, av in seq:
            if op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
                return True
            if op in _REPEATS:
                low, high, body = av
                single = len(body) == 1 and body[0][0] in _CHAR_OPS
                if low != high or high > _FAST_FIXED_MAX or not single:
                    return True
            elif op is _sre.BRANCH and self.ambiguous(av[1], self.alphabet):
                return True
            if any(self.needs_worker(sub) for sub in _children(op, av)):
                return True
        return False


# ---- worker processes -----------------------------------------------------------

# Runs with ``python -I -S`` (standard library only) and scans like ``_scan``.
# It writes one ready byte first, so interpreter start-up is not counted against a scan.
_WORKER_SOURCE = r"""
import json, re, struct, sys
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
stdout.write(b"R")
stdout.flush()
compiled = {}
def read(n):
    data = b""
    while len(data) < n:
        part = stdin.read(n - len(data))
        if not part:
            raise SystemExit(0)
        data += part
    return data
while True:
    head_len, text_len = struct.unpack(">II", read(8))
    head = json.loads(read(head_len))
    text = read(text_len).decode("utf-8", "surrogatepass")
    key = (head["pattern"], head["flags"])
    if key not in compiled:
        if len(compiled) > 64:
            compiled.clear()
        compiled[key] = re.compile(*key)
    regex = compiled[key]
    total, lines = 0, []
    for number, line in enumerate(text.split("\n"), start=1):
        if regex.search(line):
            total += 1
            if len(lines) < head["max_lines"]:
                lines.append([number, line[: head["preview"]]])
    body = json.dumps([total, lines], ensure_ascii=False).encode("utf-8", "surrogatepass")
    stdout.write(struct.pack(">I", len(body)) + body)
    stdout.flush()
"""


def _read_exact(stream, size: int) -> bytes:
    data = b""
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError("worker exited")
        data += part
    return data


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        proc.kill()


class _Worker:
    """One matcher process, used by one thread at a time."""

    START_TIMEOUT_S = 15.0

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def _process(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self.close()
        if not sys.executable:
            raise PatternError(_UNAVAILABLE)
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and source, no shell; NOSONAR(S4721)
                [sys.executable, "-I", "-S", "-c", _WORKER_SOURCE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise PatternError(_UNAVAILABLE) from exc
        self._proc = proc
        timer = threading.Timer(self.START_TIMEOUT_S, _kill, args=(proc,))
        timer.daemon = True
        timer.start()
        try:
            ready = _read_exact(proc.stdout, 1) == b"R"
        except (OSError, EOFError):
            ready = False
        finally:
            timer.cancel()
        if not ready:
            self.close()
            raise PatternError(_UNAVAILABLE)
        return proc

    def grep(self, regex: re.Pattern, text: str, max_lines: int, timeout: float) -> tuple[int, list[dict]]:
        proc = self._process()
        head = json.dumps(
            {"pattern": regex.pattern, "flags": regex.flags, "max_lines": max_lines, "preview": LINE_PREVIEW}
        ).encode("utf-8")
        body = text.encode("utf-8", "surrogatepass")
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            _kill(proc)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            proc.stdin.write(struct.pack(">II", len(head), len(body)) + head + body)
            proc.stdin.flush()
            size = struct.unpack(">I", _read_exact(proc.stdout, 4))[0]
            total, lines = json.loads(_read_exact(proc.stdout, size).decode("utf-8", "surrogatepass"))
        except (OSError, ValueError, EOFError, struct.error) as exc:
            self.close()
            if expired.is_set():
                raise PatternTimeout(
                    f"正则执行超时（单篇文档超过 {timeout:g} 秒），可能过于复杂或文字太长，请简化正则或改用普通文字查找"
                ) from None
            raise PatternError("正则执行失败，请简化后重试") from exc
        finally:
            timer.cancel()
        if expired.is_set():  # answered at the deadline; the process is being killed
            self.close()
        return total, [{"line_number": number, "text": line} for number, line in lines]

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        _kill(proc)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=2)
        for stream in (proc.stdin, proc.stdout):
            with contextlib.suppress(OSError, ValueError):
                stream.close()


class _WorkerPool:
    """A few worker processes plus a per-pattern time budget.

    A pattern that timed out, or used more than ``GREP_BUDGET_S`` of worker
    time within a minute, fails fast for a minute, so a grep over hundreds of
    documents cannot keep the workers busy with it.
    """

    WINDOW_S = 60.0

    def __init__(self, size: int) -> None:
        self._all = [_Worker() for _ in range(size)]
        self._idle: queue.LifoQueue[_Worker] = queue.LifoQueue()
        for worker in self._all:
            self._idle.put(worker)
        self._lock = threading.Lock()
        self._usage: dict[tuple, list[float]] = {}  # key -> [window start, seconds used, blocked until]

    def _entry(self, key: tuple, now: float) -> list[float]:
        entry = self._usage.get(key)
        if entry is None or (now - entry[0] > self.WINDOW_S and entry[2] <= now):
            if len(self._usage) > 512:
                self._usage.clear()
            entry = self._usage[key] = [now, 0.0, 0.0]
        return entry

    def _check(self, key: tuple) -> None:
        with self._lock:
            now = time.monotonic()
            if self._entry(key, now)[2] > now:
                raise PatternTimeout(_PAUSED)

    def grep(self, regex: re.Pattern, text: str, max_lines: int) -> tuple[int, list[dict]]:
        key = (regex.pattern, regex.flags)
        self._check(key)
        try:
            worker = self._idle.get(timeout=GREP_QUEUE_TIMEOUT_S)
        except queue.Empty:
            raise PatternTimeout(_BUSY) from None
        try:
            self._check(key)  # it may have timed out for another caller while this one waited
            started = time.monotonic()
            timed_out = False
            try:
                return worker.grep(regex, text, max_lines, GREP_TIMEOUT_S)
            except PatternTimeout:
                timed_out = True
                raise
            finally:
                now = time.monotonic()
                with self._lock:
                    entry = self._entry(key, now)
                    entry[1] += now - started
                    if timed_out or entry[1] > GREP_BUDGET_S:
                        entry[2] = now + self.WINDOW_S
        finally:
            self._idle.put(worker)

    def close(self) -> None:
        for worker in self._all:
            worker.close()


_pool_obj: _WorkerPool | None = None
_pool_lock = threading.Lock()


def _pool() -> _WorkerPool:
    global _pool_obj
    with _pool_lock:
        if _pool_obj is None:
            _pool_obj = _WorkerPool(GREP_WORKERS)
            atexit.register(_pool_obj.close)
        return _pool_obj


_executor_obj: concurrent.futures.ThreadPoolExecutor | None = None


def grep_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Threads for grep scans, apart from the event loop's default pool.

    A scan may wait seconds on a worker process; on the default pool that wait
    also held up file reads and DNS lookups (``loop.getaddrinfo``).
    """

    global _executor_obj
    with _pool_lock:
        if _executor_obj is None:
            _executor_obj = concurrent.futures.ThreadPoolExecutor(GREP_THREADS, thread_name_prefix="kb-grep")
        return _executor_obj
