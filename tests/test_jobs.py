"""Job lifecycle: status follows from the submit time, so jobs survive a server restart."""

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import mock_gkn_hpc.server as server


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def hpc(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(server.time, "time", clock)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("MOCK_GKN_HPC_CLUSTER_FILES", str(tmp_path / "no_cluster_files.txt"))
    deck = tmp_path / "job" / "runscript.ans"
    deck.parent.mkdir()
    deck.write_text("/com, nothing to read\n")
    return clock, deck


def _submit(deck: Path) -> dict:
    return asyncio.run(server.submit_ansys_run(input_file=deck, version="2024r1", np=8, product="meba"))


def test_pending_then_ready_without_a_running_process(hpc):
    clock, deck = hpc
    job = _submit(deck)
    assert asyncio.run(server.job_status(job["job_id"]))["status"] == "pending"
    clock.t += server.MOCK_RUN_SECONDS + 1
    # Nothing ran in between: the status and files follow from the submit time alone
    status = asyncio.run(server.job_status(job["job_id"]))
    assert status["status"] == "ready"
    assert "Wrote results.rst" in server.ansys_log(job["job_id"])
    assert server.ansys_results(job["job_id"])
    assert '"submitted_ts"' in server.ansys_meta(job["job_id"])


def test_list_jobs_after_restart(hpc):
    clock, deck = hpc
    job = _submit(deck)
    clock.t += server.MOCK_RUN_SECONDS + 1
    # A new server process sees the same job store
    assert [j["status"] for j in server.list_jobs() if j["job_id"] == job["job_id"]] == ["ready"]


def test_partial_log_while_pending(hpc):
    clock, deck = hpc
    job = _submit(deck)
    clock.t += 3
    log = server.ansys_log(job["job_id"])
    assert "starting" in log and "Wrote results.rst" not in log


def test_expired_after_ttl(hpc):
    clock, deck = hpc
    job = _submit(deck)
    clock.t += server.MOCK_RUN_SECONDS + server.JOB_TTL_SECONDS + 5
    assert asyncio.run(server.job_status(job["job_id"]))["status"] == "expired"


def test_delete_results_is_per_job(hpc):
    clock, deck = hpc
    a, b = _submit(deck), _submit(deck)
    server.delete_results(a["job_id"])
    remaining = {j["job_id"] for j in server.list_jobs()}
    assert remaining == {b["job_id"]}
    with pytest.raises(ToolError):
        server.delete_results("nonexistent")


SOLVING_DECK = """\
*dim,lc_id,array,3
lc_id(1)=2,20,34
*do,i,1,3
    /input,limit_load_%lc_id(i)%,inp,limit_loads
    solve
*enddo
"""


def _stage_loads(deck: Path, text: str) -> None:
    loads = deck.parent / "limit_loads"
    loads.mkdir(exist_ok=True)
    for case in (2, 20, 34):
        (loads / f"limit_load_{case}.inp").write_text("/com\n")
    deck.write_text(text)


def test_log_and_results_follow_the_deck(hpc):
    clock, deck = hpc
    _stage_loads(deck, SOLVING_DECK)
    job = _submit(deck)
    clock.t += server.MOCK_RUN_SECONDS + 1
    log = server.get_job_log(job["job_id"])["log"]
    assert "ANSYS Mechanical 2024R1 starting" in log
    assert "License: product 'meba' checked out" in log
    steps = [line for line in log.splitlines() if "LOAD STEP" in line]
    assert len(steps) == 3 and "limit_load_34.inp" in steps[-1]
    assert "Solution complete. 3 load step(s) solved." in log
    results = server.get_job_results(job["job_id"])["results"]
    assert "Load steps:           3" in results and "Version:   2024r1" in results


def test_results_not_ready_is_a_normal_answer(hpc):
    clock, deck = hpc
    job = _submit(deck)
    answer = server.get_job_results(job["job_id"])
    assert answer["status"] == "pending" and answer["results"] is None


def test_failed_job_is_a_normal_result(hpc):
    clock, deck = hpc
    deck.write_text("/input,limit_load_2,inp,limit_loads\nsolve\n")  # file not staged
    job = _submit(deck)
    assert job["status"] == "failed"
    assert "limit_load_2.inp does not exist" in job["errors"]
    assert server.list_jobs()[0]["status"] == "failed"
    assert "does not exist" in server.get_job_log(job["job_id"])["log"]


def test_no_solve_is_flagged_in_the_log(hpc):
    clock, deck = hpc
    _stage_loads(deck, "/input,limit_load_2,inp,limit_loads\n")
    job = _submit(deck)
    clock.t += server.MOCK_RUN_SECONDS + 1
    assert "No SOLVE command was executed" in server.get_job_log(job["job_id"])["log"]
