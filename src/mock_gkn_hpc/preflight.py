"""Pre-flight check of an Ansys runscript: does every file the deck reads exist for the job?

A real solve fails at the first /INPUT or CDREAD whose file the job cannot read. The mock has no
solver, so `check_deck` walks the APDL once (parameters, arrays, *DO loops, macros and %...%
substitution) and lists the files the deck would read that the job cannot see.

What a job can see:
- the staged directory: the runscript's own folder, with relative paths resolved against it;
- the cluster's shared /project storage, described by a file list (`cluster_files.txt`, or the
  file named by MOCK_GKN_HPC_CLUSTER_FILES).

Anything else, e.g. an absolute path on the submitting workstation outside the staged
directory, does not exist on the cluster.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CLUSTER_ROOT = "/project/"
DEFAULT_CLUSTER_FILES = Path(__file__).parent / "cluster_files.txt"
MAX_STATEMENTS = 100_000  # guards runaway loops

_PERCENT = re.compile(r"%([^%\s]+)%")
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\(\s*([^)]*)\s*\))?\s*=\s*(.*)$")
_NUMERIC_EXPR = re.compile(r"^[0-9eE+\-*/(). ]+$")


@dataclass
class FileRef:
    command: str  # "/INPUT" or "CDREAD"
    path: str  # as the solver would open it
    line: int


@dataclass
class DeckCheck:
    files: list[FileRef] = field(default_factory=list)
    missing: list[FileRef] = field(default_factory=list)
    unresolved: list[tuple[int, str]] = field(default_factory=list)  # (line, statement)

    @property
    def ok(self) -> bool:
        return not self.missing


def load_cluster_files() -> set[str]:
    """Absolute paths that exist on the cluster's shared /project storage."""
    path = Path(os.environ.get("MOCK_GKN_HPC_CLUSTER_FILES") or DEFAULT_CLUSTER_FILES)
    if not path.exists():
        return set()
    return {
        str(PurePosixPath(line.strip()))
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


class _Unresolved(Exception):
    pass


class _Deck:
    """Just enough APDL to know which files a deck reads."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.params: dict[str, dict[int, object]] = {}
        self.macros: dict[str, list[tuple[int, str]]] = {}
        self.refs: list[FileRef] = []
        self.unresolved: list[tuple[int, str]] = []
        self.steps = 0

    # --- values ---------------------------------------------------------------

    def lookup(self, name: str, index: int = 1) -> object:
        values = self.params.get(name.lower())
        if values is None or index not in values:
            raise _Unresolved(name)
        return values[index]

    def value(self, expr: str, args: dict[str, str]) -> object:
        expr = expr.strip()
        if len(expr) >= 2 and expr[0] == expr[-1] == "'":
            return self.substitute(expr[1:-1], args)
        if (m := re.fullmatch(r"(?i)str_?cat\((.*),(.*)\)", expr)):
            return f"{self.value(m[1], args)}{self.value(m[2], args)}"
        if expr.lower() in args:
            return args[expr.lower()]
        if (m := re.fullmatch(r"([A-Za-z_]\w*)\s*\((.+)\)", expr)):
            return self.lookup(m[1], int(self.number(m[2], args)))
        if re.fullmatch(r"[A-Za-z_]\w*", expr):
            return self.lookup(expr)
        return self.number(expr, args)

    def number(self, expr: str, args: dict[str, str]) -> float:
        def ref(m: re.Match) -> str:
            name, index = m[1], m[2]
            if index is not None:
                val = self.lookup(name, int(self.number(index, args)))
            elif name.lower() in args:
                val = args[name.lower()]
            else:
                val = self.lookup(name)
            return str(float(val))

        text = expr
        for _ in range(10):  # innermost array lookups first
            new = re.sub(r"([A-Za-z_]\w*)\s*(?:\(([^()]*)\))?", ref, text)
            if new == text:
                break
            text = new
        if not _NUMERIC_EXPR.fullmatch(text):
            raise _Unresolved(expr)
        try:
            return float(eval(text, {"__builtins__": {}}, {}))  # digits and operators only
        except Exception as exc:
            raise _Unresolved(expr) from exc

    @staticmethod
    def fmt(val: object) -> str:
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val)

    def substitute(self, text: str, args: dict[str, str]) -> str:
        return _PERCENT.sub(lambda m: self.fmt(self.value(m[1], args)), text)

    # --- execution ------------------------------------------------------------

    def run(self) -> None:
        self.block([(i + 1, line) for i, line in enumerate(self.lines)], {})

    def block(self, stmts: list[tuple[int, str]], args: dict[str, str]) -> None:
        i = 0
        while i < len(stmts):
            lineno, raw = stmts[i]
            stmt = _strip_comment(raw)
            head = stmt.split(",")[0].strip().lower()
            if head in ("*do", "*create"):
                end = "*enddo" if head == "*do" else "*end"
                body, i = _collect(stmts, i, head, end)
                if head == "*do":
                    self.do_loop(lineno, stmt, body, args)
                else:
                    name = stmt.split(",")[1].strip().lower()
                    self.macros[name] = body
                continue
            self.statement(lineno, stmt, args)
            i += 1

    def do_loop(self, lineno: int, stmt: str, body: list, args: dict[str, str]) -> None:
        fields = [f.strip() for f in stmt.split(",")]
        try:
            var = fields[1].lower()
            start, stop = self.number(fields[2], args), self.number(fields[3], args)
            step = self.number(fields[4], args) if len(fields) > 4 and fields[4] else 1.0
        except (_Unresolved, IndexError):
            self.unresolved.append((lineno, stmt))
            return
        value = start
        while (step > 0 and value <= stop) or (step < 0 and value >= stop):
            self.params[var] = {1: value}
            self.block(body, args)
            value += step

    def statement(self, lineno: int, stmt: str, args: dict[str, str]) -> None:
        self.steps += 1
        if self.steps > MAX_STATEMENTS or not stmt.strip():
            return
        fields = [f.strip() for f in stmt.split(",")]
        head = fields[0].lower()
        try:
            if head == "/input":
                self.file_ref(lineno, "/INPUT", fields[1:4], args)
            elif head == "cdread":
                self.file_ref(lineno, "CDREAD", fields[2:5], args)
            elif head in self.macros:
                macro_args = {
                    f"arg{n}": self.fmt(self.value(a, args)) for n, a in enumerate(fields[1:], 1) if a
                }
                self.block(self.macros[head], macro_args)
            elif not head.startswith(("*", "/")) and (m := _ASSIGN.match(stmt)):
                self.assign(m[1], m[2], m[3], args)
        except _Unresolved:
            self.unresolved.append((lineno, stmt.strip()))

    def assign(self, name: str, index: str | None, rhs: str, args: dict[str, str]) -> None:
        start = int(self.number(index, args)) if index else 1
        values = self.params.setdefault(name.lower(), {})
        for offset, item in enumerate(_split_values(rhs)):
            values[start + offset] = self.value(item, args)

    def file_ref(self, lineno: int, command: str, parts: list[str], args: dict[str, str]) -> None:
        parts = (parts + ["", "", ""])[:3]
        fname, ext, directory = (self.substitute(p, args).strip().strip("'") for p in parts)
        name = f"{fname}.{ext}" if ext else fname
        path = str(PurePosixPath(directory) / name) if directory else name
        self.refs.append(FileRef(command, path, lineno))


def _strip_comment(line: str) -> str:
    """Drop an APDL '!' comment that is not inside quotes."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == "'":
            quoted = not quoted
        elif ch == "!" and not quoted:
            return line[:i]
    return line


def _split_values(rhs: str) -> list[str]:
    """Split 'a, b, c' on commas outside quotes and parentheses."""
    items, depth, quoted, current = [], 0, False, ""
    for ch in _strip_comment(rhs):
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        elif not quoted and depth == 0 and ch == ",":
            items.append(current)
            current = ""
            continue
        current += ch
    items.append(current)
    return [item.strip() for item in items if item.strip()]


def _collect(stmts: list, i: int, open_kw: str, close_kw: str) -> tuple[list, int]:
    """Body of a *DO/*CREATE block starting at stmts[i], and the index after its end."""
    depth, body = 1, []
    i += 1
    while i < len(stmts):
        head = _strip_comment(stmts[i][1]).split(",")[0].strip().lower()
        if head == open_kw:
            depth += 1
        elif head == close_kw:
            depth -= 1
            if depth == 0:
                return body, i + 1
        body.append(stmts[i])
        i += 1
    return body, i


def check_deck(deck_path: Path, cluster_files: set[str] | None = None) -> DeckCheck:
    """Files the deck reads, and which of them the job cannot see."""
    cluster_files = load_cluster_files() if cluster_files is None else cluster_files
    staged = deck_path.resolve().parent
    deck = _Deck(deck_path.read_text(errors="replace").splitlines())
    deck.run()

    result = DeckCheck(files=deck.refs, unresolved=deck.unresolved)
    seen: set[str] = set()
    for ref in deck.refs:
        if ref.path in seen:
            continue
        seen.add(ref.path)
        if not _exists(ref.path, staged, cluster_files):
            result.missing.append(ref)
    return result


def _exists(path: str, staged: Path, cluster_files: set[str]) -> bool:
    if path.startswith(CLUSTER_ROOT):
        return str(PurePosixPath(path)) in cluster_files
    local = Path(path) if Path(path).is_absolute() else staged / path
    local = local.resolve()
    # Only the staged directory travels with the job
    return local.is_relative_to(staged) and local.is_file()


def format_errors(check: DeckCheck, staged: Path) -> str:
    """Ansys-style error text for the files the job cannot read."""
    lines = []
    for ref in check.missing:
        lines += [
            " *** ERROR ***",
            f" {ref.command} failed (runscript line {ref.line}). File {ref.path} does not exist.",
        ]
    lines += [
        "",
        f" The job sees its staged directory ({staged}) and the cluster /project storage only.",
        " Relative paths are resolved against the staged directory.",
    ]
    return "\n".join(lines)
