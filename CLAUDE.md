# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A FastMCP server that **mocks** GKN's HPC for Ansys / Finite Element analysis. It is an external demo that lets clients exercise the full submit → poll → fetch-artifacts loop against a fake HPC, with no real solver, queue, or cluster behind it.

## Architecture

Packaged distribution under `src/mock_gkn_hpc/`. Single module `server.py` (~190 lines of FastMCP). The model is intentionally tiny:

- **Filesystem is the state store.** No in-memory registry. `RUNS_DIR/<job_id>/` exists ⇒ job submitted; `results.rst` present ⇒ ready; older than `JOB_TTL_SECONDS` ⇒ expired. `_job_status()` derives status from directory + `results.rst` mtime — `log.txt` is *not* a readiness sentinel because it's created at submit time and grown during the run.
- **`RUNS_DIR` defaults to `gettempdir()/mock-gkn-hpc`.** Overridable via `--runs-dir` CLI flag or `MOCK_GKN_HPC_RUNS_DIR` env var. Set in `main()` before `mcp.run()` — the module-level `RUNS_DIR` is reassigned, so all tools/resources see the override.
- **Async submit, streaming log, terminal results.** `submit_ansys_run` returns immediately with a `job_id` and two `ansys://...` URIs. A background `asyncio.create_task(_finish(job_id))` walks `LOG_SCHEDULE` (a list of `(sleep, line)` tuples summing to `MOCK_RUN_SECONDS = 30s`) appending each line to `log.txt`, then renders `results.rst` from `src/mock_gkn_hpc/templates/results.rst.tmpl` with current date/time/job_id substituted in.
- **Resources are templated**: `ansys://{job_id}/log` and `ansys://{job_id}/results`. The log resource serves whatever has been written so far — clients can poll it to watch progress mid-run. The results resource 404s ("not found or still running") until `_finish` writes it at t≈30s. Both self-delete + 404 when past TTL.
- **`age_seconds` in `list_jobs` is intentionally inconsistent**: time-since-submit while pending (dir mtime), time-since-completion once ready (`results.rst` mtime). Not a bug — derives from the dir-vs-file state model.
- `submit_ansys_run`'s `input_file` and `version` are part of the tool schema for client realism but unused by the mock.

The `templates/` directory ships *inside* the wheel (`hatchling` includes everything under `src/mock_gkn_hpc/`), so `Path(__file__).parent / "templates"` resolves identically in editable and installed mode.

## Dev workflow

The server is registered via project-scoped `.mcp.json` as a stdio server invoking `uv run mock-gkn-hpc` (the entry-point script that `uv sync` installs in editable mode). Claude Code spawns it on connect; **after every edit to `server.py`, manually reconnect via `/mcp` in Claude Code** to pick up the change.

Avoid `fastmcp call ... submit_ansys_run`: each call is a one-shot subprocess that exits before the 30s `_finish()` background task fires, leaving zombie pending jobs in `RUNS_DIR`. Either go through Claude Code (long-lived stdio session) or write a client that holds the connection open past `MOCK_RUN_SECONDS`.

Useful commands:

```bash
uv sync                                      # editable install
uv run mock-gkn-hpc --help                   # show CLI flags
uv run mock-gkn-hpc                          # run the server over stdio
uv build                                     # produce wheel + sdist in dist/
uvx --from ./dist/mock_gkn_hpc-*.whl mock-gkn-hpc --help   # smoke-test the built wheel
```

No tests, no linter configured.

## Gotchas

- Python ≥ 3.14. Per global guidance, use 3.14 syntax — no `from typing import Optional/List/Dict`.
