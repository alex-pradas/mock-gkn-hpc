"""Pre-flight check of an Ansys runscript: does every file the deck reads exist for the job?

A real solve fails at the first /INPUT or CDREAD whose file the job cannot read. The mock has no
solver, so `check_deck` walks the APDL once and lists the files the deck would read that the job
cannot see, plus the deck errors that stop a file name from resolving.

APDL covered (the subset a runscript needs to name its input files):
- scalar parameters (numbers, or character values of up to 32 characters) and *SET;
- array parameters, which must be dimensioned with *DIM before values are assigned, unless
  defined completely with an implied (colon) loop such as a(1:3)=1,2,3:
  - ARRAY/TABLE: numbers, indexed (row, column, plane);
  - CHAR: character values of up to 8 characters, indexed like ARRAY;
  - STRING: character strings of IMAX characters (rounded up to a multiple of 8, at most 248);
    the first subscript is the character position, the others pick the string, so
    name(1,j) is the j-th string and name(5) is the first string from its 5th character on;
- *DO loops, *CREATE macros called by name with ARG1..ARG9, %...% substitution, and the
  functions NINT, INT, ABS, CHRVAL, STRCAT.

What a job can see:
- the staged directory: the runscript's own folder, with relative paths resolved against it;
- the cluster's shared /project storage, described by a file list (`cluster_files.txt`, or the
  file named by MOCK_GKN_HPC_CLUSTER_FILES).

Anything else, e.g. an absolute path on the submitting workstation outside the staged
directory, does not exist on the cluster. A file read whose name cannot be resolved with the
subset above is reported as an error rather than skipped.
"""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CLUSTER_ROOT = "/project/"
DEFAULT_CLUSTER_FILES = Path(__file__).parent / "cluster_files.txt"
MAX_STATEMENTS = 100_000  # guards runaway loops
SCALAR_CHARS = 32  # character value of a scalar parameter
CHAR_CHARS = 8  # element of a CHAR array
STRING_MAX = 248  # IMAX limit of a STRING array

_PERCENT = re.compile(r"%([^%\s]+)%")
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\(\s*([^)]*)\s*\))?\s*=\s*(.*)$")
_NAME = re.compile(r"[A-Za-z_]\w*")
_NUMERIC_EXPR = re.compile(r"^[0-9eE+\-*/(). ]+$")
_FUNCTIONS = {"nint": lambda x: float(math.floor(x + 0.5)), "int": lambda x: float(int(x)), "abs": abs}


@dataclass
class FileRef:
    command: str  # "/INPUT" or "CDREAD"
    path: str  # as the solver would open it
    line: int


@dataclass
class DeckCheck:
    files: list[FileRef] = field(default_factory=list)
    missing: list[FileRef] = field(default_factory=list)
    # Deck errors that stop the job, as (line, message): undimensioned arrays, file reads whose
    # name cannot be resolved
    errors: list[tuple[int, str]] = field(default_factory=list)
    # Things worth telling the user without failing the job, as (line, message)
    notes: list[tuple[int, str]] = field(default_factory=list)
    # SOLVE commands in execution order, with the last /INPUT file read before each
    solves: list[tuple[int, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.errors

    @property
    def unresolved(self) -> list[tuple[int, str]]:  # kept for callers of 0.5.0
        return self.errors


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


class _DeckError(Exception):
    pass


@dataclass
class _Param:
    kind: str  # "scalar", "array" (ARRAY/TABLE), "char" or "string"
    dims: tuple[int, int, int] = (1, 1, 1)  # STRING: (characters, strings, planes)
    value: object = None  # scalar value
    cells: dict = field(default_factory=dict)  # array/char: {(i, j, k): v}; string: {(j, k): str}


class _Deck:
    """Just enough APDL to know which files a deck reads."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.params: dict[str, _Param] = {}
        self.macros: dict[str, list[tuple[int, str]]] = {}
        self.refs: list[FileRef] = []
        self.solves: list[tuple[int, str | None]] = []
        self.errors: list[tuple[int, str]] = []
        self.notes: list[tuple[int, str]] = []
        self.lineno = 0
        self.steps = 0

    # --- values ---------------------------------------------------------------

    def subscripts(self, text: str, args: dict[str, str]) -> list[int]:
        return [int(self.number(s, args)) if s.strip() else 1 for s in _split_values(text, keep_empty=True)]

    def lookup(self, name: str, subs: list[int] | None) -> object:
        param = self.params.get(name.lower())
        if param is None:
            raise _Unresolved(name)
        if param.kind == "scalar":
            return param.value
        subs = (subs or [1]) + [1, 1]
        if param.kind == "string":
            text = param.cells.get((subs[1], subs[2]), "")
            return text[subs[0] - 1:].rstrip()
        return param.cells.get(tuple(subs[:3]), 0.0 if param.kind == "array" else "")

    def value(self, expr: str, args: dict[str, str]) -> object:
        expr = expr.strip()
        if len(expr) >= 2 and expr[0] == expr[-1] == "'":
            return self.substitute(expr[1:-1], args)
        if (m := re.fullmatch(r"(?i)(str_?cat|chrval)\s*\((.*)\)", expr)):
            parts = _split_values(m[2])
            if m[1].lower() == "chrval":
                return self.fmt(self.number(parts[0], args))
            return "".join(str(self.value(p, args)).rstrip() for p in parts)
        if expr.lower() in args:
            return args[expr.lower()]
        if (m := re.fullmatch(r"([A-Za-z_]\w*)\s*\((.+)\)", expr)) and m[1].lower() not in _FUNCTIONS:
            return self.lookup(m[1], self.subscripts(m[2], args))
        if _NAME.fullmatch(expr):
            return self.lookup(expr, None)
        return self.number(expr, args)

    def number(self, expr: str, args: dict[str, str]) -> float:
        def ref(m: re.Match) -> str:
            name, inner = m[1], m[2]
            if name.lower() in _FUNCTIONS:  # applied once its argument is a number
                if inner is None:
                    return m[0]
                return str(_FUNCTIONS[name.lower()](self.number(inner, args)))
            if inner is not None:
                val = self.lookup(name, self.subscripts(inner, args))
            elif name.lower() in args:
                val = args[name.lower()]
            else:
                val = self.lookup(name, None)
            return str(float(val))

        text = expr
        for _ in range(10):  # innermost references first
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
                    name = _split_fields(stmt)[1].strip().lower()
                    self.macros[name] = body
                continue
            self.statement(lineno, stmt, args)
            i += 1

    def do_loop(self, lineno: int, stmt: str, body: list, args: dict[str, str]) -> None:
        fields = _split_fields(stmt)
        try:
            var = fields[1].lower()
            start, stop = self.number(fields[2], args), self.number(fields[3], args)
            step = self.number(fields[4], args) if len(fields) > 4 and fields[4] else 1.0
        except (_Unresolved, IndexError):
            self.errors.append((lineno, f"*DO loop limits cannot be evaluated: {stmt.strip()}"))
            return
        value = start
        while (step > 0 and value <= stop) or (step < 0 and value >= stop):
            self.params[var] = _Param("scalar", value=value)
            self.block(body, args)
            value += step

    def statement(self, lineno: int, stmt: str, args: dict[str, str]) -> None:
        self.steps += 1
        self.lineno = lineno
        if self.steps > MAX_STATEMENTS or not stmt.strip():
            return
        fields = _split_fields(stmt)
        head = fields[0].lower()
        try:
            if head == "/input":
                self.file_ref(lineno, "/INPUT", fields[1:4], args)
            elif head == "solve":
                last = next((r.path for r in reversed(self.refs) if r.command == "/INPUT"), None)
                self.solves.append((lineno, last))
            elif head == "cdread":
                self.file_ref(lineno, "CDREAD", fields[2:5], args)
            elif head == "*dim":
                self.dim(fields, args)
            elif head == "*set" and len(fields) > 1:
                if (m := re.fullmatch(r"([A-Za-z_]\w*)\s*(?:\((.*)\))?", fields[1])):
                    self.assign(m[1], m[2], ",".join(fields[2:]), args)
            elif head in self.macros:
                macro_args = {
                    f"arg{n}": self.fmt(self.value(a, args)) for n, a in enumerate(fields[1:], 1) if a
                }
                self.block(self.macros[head], macro_args)
            elif not head.startswith(("*", "/")) and (m := _ASSIGN.match(stmt)):
                self.assign(m[1], m[2], m[3], args)
        except _DeckError as exc:
            self.errors.append((lineno, str(exc)))
        except _Unresolved:
            pass  # a parameter that names no file; a file read that depends on it fails in file_ref

    def dim(self, fields: list[str], args: dict[str, str]) -> None:
        name = fields[1].strip().lower()
        kind = (fields[2].strip().lower() if len(fields) > 2 else "") or "array"
        sizes = []
        for f in fields[3:6]:
            sizes.append(int(self.number(f, args)) if f.strip() else 1)
        sizes = (sizes + [1, 1, 1])[:3]
        if kind == "string":
            sizes[0] = min(STRING_MAX, -(-max(sizes[0], 1) // 8) * 8)
            self.params[name] = _Param("string", tuple(sizes))
        elif kind == "char":
            self.params[name] = _Param("char", tuple(sizes))
        else:  # ARRAY, TABLE
            self.params[name] = _Param("array", tuple(sizes))

    def assign(self, name: str, index: str | None, rhs: str, args: dict[str, str]) -> None:
        key = name.lower()
        values = [self.value(item, args) for item in _split_values(rhs)]
        if not values:
            return
        if index is None or not index.strip():
            val = values[0]
            if isinstance(val, str) and len(val) > SCALAR_CHARS:
                self.notes.append((self.lineno, (
                    f"{name.upper()} = a {len(val)}-character value: character parameters hold "
                    f"{SCALAR_CHARS} characters, so it was cut to '{val[:SCALAR_CHARS]}'. "
                    "Use *DIM,...,STRING for longer text."
                )))
                val = val[:SCALAR_CHARS]
            self.params[key] = _Param("scalar", value=val)
            return
        param = self.params.get(key)
        if param is None or param.kind == "scalar":
            if ":" in index:  # implied (colon) loop defines the array
                lo = int(self.number(index.split(":")[0], args))
                param = _Param("char" if isinstance(values[0], str) else "array", (lo + len(values) - 1, 1, 1))
                self.params[key] = param
                subs = [lo, 1, 1]
            else:
                raise _DeckError(
                    f"Array parameter {name.upper()} must be dimensioned (*DIM) before being "
                    "assigned values."
                )
        else:
            subs = (self.subscripts(index.split(":")[0] if ":" in index else index, args) + [1, 1])[:3]
        if param.kind == "string":
            for offset, val in enumerate(values):  # further values go to the following strings
                cell = (subs[1] + offset, subs[2])
                width = param.dims[0]
                text = param.cells.get(cell, "").ljust(width)
                start = subs[0] - 1
                new = str(val)
                param.cells[cell] = (text[:start] + new + text[start + len(new):])[:width]
            return
        for offset, val in enumerate(values):
            if param.kind == "char" and isinstance(val, str) and len(val) > CHAR_CHARS:
                self.notes.append((self.lineno, (
                    f"{name.upper()}: '{val}' has {len(val)} characters; CHAR array elements hold "
                    f"{CHAR_CHARS}, so it was cut to '{val[:CHAR_CHARS]}'."
                )))
                val = val[:CHAR_CHARS]
            param.cells[(subs[0] + offset, subs[1], subs[2])] = val

    def file_ref(self, lineno: int, command: str, parts: list[str], args: dict[str, str]) -> None:
        parts = (parts + ["", "", ""])[:3]
        try:
            fname, ext, directory = (self.substitute(p, args).strip().strip("'") for p in parts)
        except _Unresolved as exc:
            raise _DeckError(
                f"{command} file name cannot be resolved: parameter {str(exc).upper()} has no value "
                f"here. Statement: {','.join(parts).rstrip(',')}"
            ) from exc
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


def _split_fields(stmt: str) -> list[str]:
    """Command fields: split on commas outside quotes, parentheses and %...% substitutions."""
    fields, depth, quoted, percent, current = [], 0, False, False, ""
    for ch in stmt:
        if ch == "'" and not percent:
            quoted = not quoted
        elif ch == "%" and not quoted:
            percent = not percent
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        elif not quoted and not percent and depth == 0 and ch == ",":
            fields.append(current.strip())
            current = ""
            continue
        current += ch
    fields.append(current.strip())
    return fields


def _split_values(rhs: str, keep_empty: bool = False) -> list[str]:
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
    if keep_empty:
        return [item.strip() for item in items]
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
    """Files the deck reads, which of them the job cannot see, and deck errors."""
    cluster_files = load_cluster_files() if cluster_files is None else cluster_files
    staged = deck_path.resolve().parent
    deck = _Deck(deck_path.read_text(errors="replace").splitlines())
    deck.run()

    result = DeckCheck(files=deck.refs, errors=deck.errors, notes=deck.notes, solves=deck.solves)
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
    """Ansys-style error text for the deck errors and the files the job cannot read."""
    lines = []
    for line, message in check.errors:
        lines += [" *** ERROR ***", f" {message} (runscript line {line})"]
    for ref in check.missing:
        lines += [
            " *** ERROR ***",
            f" {ref.command} failed (runscript line {ref.line}). File {ref.path} does not exist.",
        ]
    for line, message in check.notes:
        lines += [" *** NOTE ***", f" {message} (runscript line {line})"]
    lines += [
        "",
        f" The job sees its staged directory ({staged}) and the cluster /project storage only.",
        " Relative paths are resolved against the staged directory.",
    ]
    return "\n".join(lines)
