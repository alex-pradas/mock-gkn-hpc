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
