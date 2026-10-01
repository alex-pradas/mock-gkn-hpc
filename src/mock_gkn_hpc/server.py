import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import FilePath

from .preflight import check_deck, format_errors

mcp = FastMCP(
    "HPC at GKN, external demo",
    instructions="Simulates the HPC for Finite Elements analyisis at GKN",
)

DEFAULT_RUNS_DIR = Path(tempfile.gettempdir()) / "mock-gkn-hpc"
RUNS_DIR = DEFAULT_RUNS_DIR
TEMPLATES_DIR = Path(__file__).parent / "templates"
JOB_TTL_SECONDS = 3600
MOCK_RUN_SECONDS = 30
MAX_WAIT_SECONDS = 120  # longest job_status wait
META_FILENAME = "meta.json"
ERROR_FILENAME = "error.txt"
START_TIME_RE = re.compile(r"^\d{8}$")  # MMDDhhmm

Product = Literal["ansys", "meba", "mechs"]

# Fixed figures of the mock model, shared by the log and results.rst
MESH_LINE = "Mesh statistics: 12847 nodes, 8432 elements, 38541 dofs"


def _write_meta(job_dir: Path, meta: dict) -> None:
    (job_dir / META_FILENAME).write_text(json.dumps(meta, indent=2))


def _read_meta(job_dir: Path) -> dict | None:
    path = job_dir / META_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _banner(meta: dict) -> list[str]:
    priority = "high" if meta.get("high_prio") else "normal"
    return [
        f"qansys{meta['version']} starting; job={meta['job_name']}, "
        f"np={meta['np']}, product={meta['product']}, priority={priority}",
        f"Input:  {meta['input_file']}",
        f"Output: {meta.get('output_file') or '<auto>'}",
        f"Scheduled start: {meta.get('start_time') or 'now'}",
        "",
    ]


def _render_results(job_id: str, job_dir: Path, dst: Path) -> None:
    tmpl = (TEMPLATES_DIR / "results.rst.tmpl").read_text()
    meta = _read_meta(job_dir) or {}
    now = datetime.fromtimestamp(float(meta.get("submitted_ts", time.time())) + MOCK_RUN_SECONDS)
    dst.write_text(tmpl.format(
        run_date=now.strftime("%Y-%m-%d"),
        run_time=now.strftime("%H:%M:%S"),
        job_id=job_id,
        job_name=meta.get("job_name") or "-",
        input_file=meta.get("input_file") or "-",
        np=meta.get("np", "-"),
        product=meta.get("product") or "-",
        start_time=meta.get("start_time") or "now",
        priority="high" if meta.get("high_prio") else "normal",
        version=meta.get("version") or "-",
        load_steps=len(meta.get("solves") or [None]),
    ))


def _elapsed(meta: dict) -> float:
    """Seconds since submission (jobs are stateless: status follows from the submit time)."""
    return time.time() - float(meta.get("submitted_ts", 0.0))


def _timeline(meta: dict) -> list[tuple[float, str]]:
    """(seconds after submission, line) of the solver log, built from what the deck does: the
    requested version and product, every file it reads, and one load step per SOLVE."""
    files = meta.get("files_read") or []
    solves = meta.get("solves")
    if solves is None:  # jobs submitted before 0.6.0
        solves = [{"line": None, "load_file": None}]
    run = MOCK_RUN_SECONDS
    out: list[tuple[float, str]] = [
        (0.0, f"ANSYS Mechanical {str(meta.get('version', '')).upper()} starting..."),
        (0.02 * run, f"License: product '{meta.get('product')}' checked out"),
        (0.03 * run, f"Reading input file: {Path(meta.get('input_file', '')).name}"),
    ]
    for n, ref in enumerate(files):
        out.append((0.04 * run + 0.08 * run * n / max(len(files), 1), f"{ref['command']}: {ref['path']}"))
    out += [(0.13 * run, MESH_LINE), (0.15 * run, "--- SOLUTION ---"), (0.15 * run, "Solver: Sparse Direct")]
    if not solves:
        out.append((0.2 * run, " *** WARNING *** No SOLVE command was executed: no load steps solved."))
    for k, solve in enumerate(solves, 1):
        t = 0.2 * run + 0.7 * run * k / len(solves)
        load = f" (load file: {Path(solve['load_file']).name})" if solve.get("load_file") else ""
        out.append((t, f"*** LOAD STEP {k} SUBSTEP 1 COMPLETED. CUM ITER = {k} ***{load}"))
    out += [
        (0.92 * run, "Solver complete; max residual = 4.21e-09"),
        (0.93 * run, "--- POSTPROCESSING ---"),
        (0.95 * run, "Writing results database..."),
        (0.98 * run, "Wrote results.rst"),
        (run, f"Solution complete. {len(solves)} load step(s) solved."),
    ]
    return out


def _log_lines(meta: dict, elapsed: float) -> list[str]:
    """The log as written so far, `elapsed` seconds into the run."""
    return list(_banner(meta)) + [line for t, line in _timeline(meta) if t <= elapsed]


def _refresh(job_dir: Path) -> None:
    """Bring a job's files up to date with its elapsed time. Jobs need no running process, so
    they survive a server restart: a new server picks up where the old one left off."""
    meta = _read_meta(job_dir)
    if meta is None or (job_dir / ERROR_FILENAME).exists():
        return
    elapsed = _elapsed(meta)
    (job_dir / "log.txt").write_text("\n".join(_log_lines(meta, elapsed)) + "\n")
    results = job_dir / "results.rst"
    if elapsed >= MOCK_RUN_SECONDS and not results.exists():
        _render_results(meta["job_id"], job_dir, results)


def _job_status(job_dir: Path) -> tuple[str, float]:
    """(status, age in seconds): pending until MOCK_RUN_SECONDS after submission, then ready;
    expired JOB_TTL_SECONDS after finishing; failed if the deck was rejected."""
    meta = _read_meta(job_dir) or {}
    elapsed = _elapsed(meta) if meta else time.time() - job_dir.stat().st_mtime
    if (job_dir / ERROR_FILENAME).exists():
        return "failed", elapsed
    if elapsed < MOCK_RUN_SECONDS:
        return "pending", elapsed
    age = elapsed - MOCK_RUN_SECONDS
    return ("expired" if age > JOB_TTL_SECONDS else "ready"), age


@mcp.tool
async def submit_ansys_run(
    input_file: FilePath,
    version: str = "2025r1",
    job_name: str | None = None,
    output_file: str | None = None,
    np: int = 4,
    product: Product = "ansys",
    start_time: str | None = None,
    high_prio: bool = False,
) -> dict:
    """Submits run to HPC. Returns immediately with a job_id; the job runs for
    ~30s. Wait for it with job_status(job_id, wait_s), then read the log and
    results at the returned URIs. Resources expire 1h after the job finishes.

    Mirrors GKN's `qansys` flags: -i (input_file), -j (job_name),
    -o (output_file), --np, -p (product), -a (start_time, MMDDhhmm),
    --highprio. -a is recorded but does not actually delay the run.

    The job sees the input file's directory (staged with the job) and the
    cluster /project storage. If the deck reads a file (/INPUT, CDREAD) that
    neither contains, or the deck has an error, the job fails at input
    processing and this tool returns status "failed" with the solver errors."""
    if start_time is not None and not START_TIME_RE.match(start_time):
        raise ValueError(
            f"start_time must be MMDDhhmm (8 digits), got {start_time!r}"
        )

    job_id = uuid.uuid4().hex[:8]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "job_name": job_name or Path(input_file).stem,
        "input_file": str(input_file),
        "output_file": output_file,
        "version": version,
        "np": np,
        "product": product,
        "start_time": start_time,
        "high_prio": high_prio,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "submitted_ts": time.time(),
    }
    _write_meta(job_dir, meta)

    # A real solve stops at the first file it cannot read. The mock has no solver, so it checks
    # the deck up front and returns the failure to the submitter, who can fix it and resubmit.
    deck = Path(input_file).resolve()
    check = check_deck(deck)
    if not check.ok:
        errors = format_errors(check, deck.parent)
        log = "\n".join(_banner(meta)) + "\nReading input file: " + deck.name + "\n" + errors + "\n"
        (job_dir / "log.txt").write_text(log)
        (job_dir / ERROR_FILENAME).write_text(errors)
        problems = []
        if check.errors:
            problems.append(f"{len(check.errors)} deck error(s)")
        if check.missing:
            problems.append(f"{len(check.missing)} file(s) read by the deck do not exist")
        # A failed job is an outcome, not a broken call: it comes back as a normal result
        return {
            "status": "failed",
            "message": f"Job {job_id} ({meta['job_name']}) failed during input processing: "
            f"{'; '.join(problems)}.",
            "errors": errors,
            "log_uri": f"ansys://{job_id}/log",
            **meta,
        }

    meta["files_read"] = [{"command": r.command, "path": r.path} for r in check.files]
    meta["solves"] = [{"line": line, "load_file": load} for line, load in check.solves]
    _write_meta(job_dir, meta)
    _refresh(job_dir)
    return {
        "status": "submitted",
        "log_uri": f"ansys://{job_id}/log",
        "results_uri": f"ansys://{job_id}/results",
        "input_files_checked": len({ref.path for ref in check.files}),
        **({"notes": [f"line {line}: {msg}" for line, msg in check.notes]} if check.notes else {}),
        **meta,
    }


@mcp.tool
def status() -> str:
    return "online"


@mcp.tool
def list_jobs() -> list[dict]:
    """Lists every submitted job with its current status (pending/ready/failed/expired) and age in seconds."""
    if not RUNS_DIR.exists():
        return []
    jobs = []
    for job_dir in sorted(RUNS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        _refresh(job_dir)
        st, age = _job_status(job_dir)
        meta = _read_meta(job_dir) or {}
        jobs.append({
            "job_id": job_dir.name,
            "job_name": meta.get("job_name"),
            "status": st,
            "age_seconds": round(age, 1),
            "np": meta.get("np"),
            "product": meta.get("product"),
            "log_uri": f"ansys://{job_dir.name}/log",
            "results_uri": f"ansys://{job_dir.name}/results",
        })
    return jobs


@mcp.tool
async def job_status(job_id: str, wait_s: float = 0) -> dict:
    """Status of one job (pending/ready/failed/expired). With wait_s > 0, waits up to wait_s
    seconds (at most 120) for a pending job to finish before answering."""
    job_dir = RUNS_DIR / job_id
    if not (job_dir / META_FILENAME).exists():
        raise ToolError(f"Unknown job {job_id}.")
    wait = min(max(wait_s, 0), MAX_WAIT_SECONDS)
    deadline = time.time() + wait
    for _ in range(int(wait) + 2):  # bounded even if the clock stalls
        _refresh(job_dir)
        st, age = _job_status(job_dir)
        if st != "pending" or time.time() >= deadline:
            break
        await asyncio.sleep(min(1.0, max(deadline - time.time(), 0.05)))
    meta = _read_meta(job_dir) or {}
    return {
        "job_id": job_id,
        "job_name": meta.get("job_name"),
        "status": st,
        "age_seconds": round(age, 1),
        "log_uri": f"ansys://{job_id}/log",
        "results_uri": f"ansys://{job_id}/results",
        "meta_uri": f"ansys://{job_id}/meta",
    }


@mcp.tool
def get_job_log(job_id: str) -> dict:
    """The job's solver log as written so far (same content as ansys://{job_id}/log)."""
    job_dir = RUNS_DIR / job_id
    if not (job_dir / META_FILENAME).exists():
        raise ToolError(f"Unknown job {job_id}.")
    _refresh(job_dir)
    st, _ = _job_status(job_dir)
    if st == "expired":
        return {"job_id": job_id, "status": st, "log": None, "message": "Log expired."}
    return {"job_id": job_id, "status": st, "log": (job_dir / "log.txt").read_text()}


@mcp.tool
def get_job_results(job_id: str) -> dict:
    """The job's results file once it is ready (same content as ansys://{job_id}/results)."""
    job_dir = RUNS_DIR / job_id
    if not (job_dir / META_FILENAME).exists():
        raise ToolError(f"Unknown job {job_id}.")
    _refresh(job_dir)
    st, _ = _job_status(job_dir)
    results = job_dir / "results.rst"
    if st != "ready" or not results.exists():
        return {"job_id": job_id, "status": st, "results": None,
                "message": f"No results: the job is {st}."}
    return {"job_id": job_id, "status": st, "results": results.read_text()}


@mcp.tool
def delete_results(job_id: str) -> dict:
    """Deletes one job's log, results and metadata. Archive anything you need first."""
    job_dir = RUNS_DIR / job_id
    if not job_dir.is_dir():
        raise ToolError(f"Unknown job {job_id}.")
    shutil.rmtree(job_dir)
    return {"deleted": job_id}


def _read_artifact(job_id: str, name: str) -> str:
    job_dir = RUNS_DIR / job_id
    if job_dir.is_dir():
        _refresh(job_dir)
    path = job_dir / name
    if not path.exists():
        raise ValueError(f"Job {job_id} {name}: not found or still running.")
    if job_dir.is_dir() and _job_status(job_dir)[0] == "expired":
        raise ValueError(f"Job {job_id} {name}: expired.")
    return path.read_text()


@mcp.resource("ansys://{job_id}/log")
def ansys_log(job_id: str) -> str:
    return _read_artifact(job_id, "log.txt")


@mcp.resource("ansys://{job_id}/results")
def ansys_results(job_id: str) -> str:
    return _read_artifact(job_id, "results.rst")


@mcp.resource("ansys://{job_id}/meta")
def ansys_meta(job_id: str) -> str:
    return _read_artifact(job_id, META_FILENAME)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Public MCP mock of GKN's HPC for Ansys/FEA analysis"
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help=f"Where to store job artifacts (default: {DEFAULT_RUNS_DIR})",
    )
    args = parser.parse_args()

    runs = args.runs_dir
    if runs is None:
        env = os.environ.get("MOCK_GKN_HPC_RUNS_DIR")
        runs = Path(env) if env else DEFAULT_RUNS_DIR

    global RUNS_DIR
    RUNS_DIR = runs

    mcp.run()


if __name__ == "__main__":
    main()
