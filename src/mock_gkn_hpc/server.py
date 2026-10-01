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
META_FILENAME = "meta.json"
ERROR_FILENAME = "error.txt"
START_TIME_RE = re.compile(r"^\d{8}$")  # MMDDhhmm

Product = Literal["ansys", "meba", "mechs"]

# (sleep_before_writing_seconds, line). Sleeps sum to MOCK_RUN_SECONDS.
LOG_SCHEDULE: list[tuple[float, str]] = [
    (0.0, "ANSYS Mechanical 2025R1 starting..."),
    (0.2, "License: ANSYS Mechanical Enterprise -- checked out"),
    (0.3, "Reading input file: model.cdb"),
    (0.5, "Mesh statistics: 12847 nodes, 8432 elements, 38541 dofs"),
    (0.5, "Material: Structural Steel (E=200 GPa, nu=0.30)"),
    (0.5, "Boundary conditions: 1 fixed support, 2 force loads"),
    (0.5, ""),
    (0.0, "--- SOLUTION ---"),
    (0.5, "Solver: Sparse Direct"),
    (0.5, "Reordering equations (METIS)..."),
    (1.0, "Equation reordering complete; matrix non-zeros = 1.84M, factor non-zeros = 8.2M"),
    (1.0, "Symbolic factorization..."),
    (2.0, "Numeric factorization (LDL^T)..."),
    (5.0, "Factor complete; peak memory = 412 MB"),
    (2.0, "Forward/back substitution..."),
    (4.0, "Solver complete; max residual = 4.21e-09"),
    (2.0, "*** LOAD STEP 1 SUBSTEP 1 COMPLETED. CUM ITER = 1 ***"),
    (2.0, ""),
    (0.0, "--- POSTPROCESSING ---"),
    (1.0, "Computing element results..."),
    (2.0, "Max von Mises stress: 187.4 MPa @ node 4521"),
    (1.0, "Max displacement: 0.842 mm @ node 9183"),
    (1.0, "Reaction force at fixed support: (-2104.3, 8.2, -45.1) N"),
    (1.0, "Writing results database..."),
    (1.5, "Wrote results.rst"),
    (0.5, "Solution complete. CPU time: 28.7s, elapsed: 30.1s"),
]


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
    now = datetime.now()
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
    ))


async def _finish(job_id: str) -> None:
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log = job_dir / "log.txt"
    meta = _read_meta(job_dir) or {}
    log.write_text("")
    with log.open("a") as f:
        for line in _banner(meta):
            f.write(line + "\n")
    for delay, line in LOG_SCHEDULE:
        if delay:
            await asyncio.sleep(delay)
        with log.open("a") as f:
            f.write(line + "\n")
    _render_results(job_id, job_dir, job_dir / "results.rst")


def _job_status(job_dir: Path) -> tuple[str, float]:
    if (job_dir / ERROR_FILENAME).exists():
        return "failed", time.time() - job_dir.stat().st_mtime
    results = job_dir / "results.rst"
    if not results.exists():
        return "pending", time.time() - job_dir.stat().st_mtime
    age = time.time() - results.stat().st_mtime
    if age > JOB_TTL_SECONDS:
        return "expired", age
    return "ready", age


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
    """Submits run to HPC. Returns immediately; log and results become
    available at the returned URIs after ~30s. Resources expire after 1h.

    Mirrors GKN's `qansys` flags: -i (input_file), -j (job_name),
    -o (output_file), --np, -p (product), -a (start_time, MMDDhhmm),
    --highprio. -a is recorded but does not actually delay the run.

    The job sees the input file's directory (staged with the job) and the
    cluster /project storage. If the deck reads a file (/INPUT, CDREAD) that
    neither contains, the job fails at input processing and this tool
    returns the solver error."""
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
        raise ToolError(
            f"Job {job_id} ({meta['job_name']}) failed during input processing: "
            f"{len(check.missing)} file(s) read by the deck do not exist.\n{errors}"
        )

    asyncio.create_task(_finish(job_id))
    return {
        "status": "submitted",
        "log_uri": f"ansys://{job_id}/log",
        "results_uri": f"ansys://{job_id}/results",
        "input_files_checked": len({ref.path for ref in check.files}),
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
def delete_results() -> dict:
    """Deletes every job's log and results files."""
    if not RUNS_DIR.exists():
        return {"deleted": 0}
    count = sum(1 for p in RUNS_DIR.iterdir() if p.is_dir())
    shutil.rmtree(RUNS_DIR)
    return {"deleted": count}


def _read_artifact(job_id: str, name: str) -> str:
    path = RUNS_DIR / job_id / name
    if not path.exists():
        raise ValueError(f"Job {job_id} {name}: not found or still running.")
    if time.time() - path.stat().st_mtime > JOB_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise ValueError(f"Job {job_id} {name}: expired.")
    return path.read_text()


@mcp.resource("ansys://{job_id}/log")
def ansys_log(job_id: str) -> str:
    return _read_artifact(job_id, "log.txt")


@mcp.resource("ansys://{job_id}/results")
def ansys_results(job_id: str) -> str:
    return _read_artifact(job_id, "results.rst")


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
