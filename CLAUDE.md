# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A FastMCP server that **mocks** GKN's HPC for Ansys / Finite Element analysis. It is an external demo that lets clients exercise the full submit → poll → fetch-artifacts loop against a fake HPC, with no real solver, queue, or cluster behind it.

## Architecture

Packaged distribution under `src/mock_gkn_hpc/`: `server.py` (FastMCP tools/resources) and `preflight.py` (runscript input check). The model is intentionally tiny:

- **Filesystem is the state store, and jobs are stateless.** No in-memory registry and no background task. `meta.json` (written at submit) holds the parameters and `submitted_ts`; `_job_status()` derives status from the elapsed time: pending for `MOCK_RUN_SECONDS`, then ready, expired `JOB_TTL_SECONDS` after finishing; `error.txt` ⇒ failed. `_refresh()` (called by `list_jobs`, `job_status` and the resources) rewrites `log.txt` up to the elapsed time and renders `results.rst` once finished, so jobs survive a server restart: a new process picks up the same job store.
- **`RUNS_DIR` defaults to `gettempdir()/mock-gkn-hpc`.** Overridable via `--runs-dir` CLI flag or `MOCK_GKN_HPC_RUNS_DIR` env var. Set in `main()` before `mcp.run()` — the module-level `RUNS_DIR` is reassigned, so all tools/resources see the override.
- **Async submit, growing log, terminal results.** `submit_ansys_run` returns immediately with a `job_id` and `ansys://...` URIs. The log is built from the job (`_timeline`): version and product from the parameters, the files the pre-flight walk read (`meta.files_read`) and one LOAD STEP per `SOLVE` (`meta.solves`), timed over `MOCK_RUN_SECONDS = 30s`; once finished, the full log and `results.rst` (from `templates/results.rst.tmpl`, dated submit + 30 s, with the load-step count). A rejected deck is a normal result with `status: "failed"`, not a `ToolError`; `get_job_log`/`get_job_results` serve the resources as tools. `job_status(job_id, wait_s)` lets a client wait in the foreground (max `MAX_WAIT_SECONDS`).
- **Resources are templated**: `ansys://{job_id}/log`, `/results` and `/meta`. The log resource serves the log so far; the results resource errors ("not found or still running") until t≈30s; all error when expired. `delete_results(job_id)` removes one job.
- **`age_seconds`** is time since submission while pending or failed, and time since completion once ready or expired.
- **Input pre-flight (`preflight.py`).** At submission, `submit_ansys_run` runs `check_deck()`: a small APDL walker (scalars and `*SET`; typed `*DIM` arrays: ARRAY/TABLE, CHAR with 8-character elements, STRING whose first subscript is the character position; implied colon loops; `*DO` loops, `*CREATE` macros with `%argN%`, `%...%` substitution; `NINT`/`INT`/`ABS`/`CHRVAL`/`STRCAT`) that records every `/INPUT`/`CDREAD` file. Command fields split on commas outside quotes, parentheses and `%...%`. Files must exist in the staged directory (input file's folder) or in the cluster file list (`cluster_files.txt`, `/project/...` paths; override with `MOCK_GKN_HPC_CLUSTER_FILES`). On a miss the job gets `error.txt` + an error log, `_job_status` reports `failed`, and the tool returns `status: "failed"` with the Ansys-style message. Deck errors fail the job too: an array assigned without `*DIM`, or a file read whose name cannot be resolved. Character values cut to 32 (scalar) or 8 (CHAR) characters become `notes`.
- `submit_ansys_run` mirrors GKN's `qansys` flags (`-i`, `-j`, `-o`, `--np`, `-p`, `-a`, `--highprio`). All are recorded in `meta.json` and surfaced in the log banner / results, but **none actually affect simulation behavior** — `-a` (start_time) is format-validated but doesn't delay the run; `--np` doesn't scale `MOCK_RUN_SECONDS`; `-p` is a `Literal["ansys", "meba", "mechs"]` enforced by Pydantic. The schema exists for client realism, not enacted semantics.

The `templates/` directory ships *inside* the wheel (`hatchling` includes everything under `src/mock_gkn_hpc/`), so `Path(__file__).parent / "templates"` resolves identically in editable and installed mode.

## Dev workflow

The server is registered via project-scoped `.mcp.json` as a stdio server invoking `uv run mock-gkn-hpc` (the entry-point script that `uv sync` installs in editable mode). Claude Code spawns it on connect; **after every edit to `server.py`, manually reconnect via `/mcp` in Claude Code** to pick up the change.

One-shot clients (`fastmcp call ...`) work: jobs are stateless, so a job submitted by one process is ready for the next one after `MOCK_RUN_SECONDS`.

Useful commands:

```bash
uv sync                                      # editable install
uv run mock-gkn-hpc --help                   # show CLI flags
uv run mock-gkn-hpc                          # run the server over stdio
uv build                                     # produce wheel + sdist in dist/
uvx --from ./dist/mock_gkn_hpc-*.whl mock-gkn-hpc --help   # smoke-test the built wheel
```

Tests: `uv run --with pytest pytest -q tests` (pre-flight check). No linter configured.

## Release process

Published to PyPI as `mock-gkn-hpc` via OIDC trusted publishing — no API tokens stored. The `Test and Publish` workflow in `.github/workflows/publish.yml` runs a smoke import on every push and auto-publishes the wheel + sdist on every GitHub Release.

To cut a new release:

1. Bump `version` in `pyproject.toml` and commit (`uv sync` to refresh `uv.lock`).
2. `gh release create vX.Y.Z --generate-notes --title "vX.Y.Z — <one-liner>"` — pushes the tag, creates the release, and triggers the workflow.
3. `gh run watch <run-id>` to confirm both `test` and `publish` jobs go green. Sigstore attestations are uploaded automatically.
4. Verify: `uvx --refresh mock-gkn-hpc --help` should pull the new version off PyPI.

The trusted-publisher binding (PyPI ↔ `alex-pradas/mock-gkn-hpc`'s `publish.yml`) is one-time setup at https://pypi.org/manage/account/publishing/ — already configured.

## Gotchas

- Python ≥ 3.14. Per global guidance, use 3.14 syntax — no `from typing import Optional/List/Dict`.
