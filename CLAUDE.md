# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A FastMCP server that **mocks** GKN's HPC for Ansys / Finite Element analysis. It is an external demo that lets clients exercise the full submit → poll → fetch-artifacts loop against a fake HPC, with no real solver, queue, or cluster behind it.

## Architecture

Packaged distribution under `src/mock_gkn_hpc/`: `server.py` (FastMCP tools/resources) and `preflight.py` (runscript input check). The model is intentionally tiny:

- **Filesystem is the state store.** No in-memory registry. `RUNS_DIR/<job_id>/` exists ⇒ job submitted; `results.rst` present ⇒ ready; older than `JOB_TTL_SECONDS` ⇒ expired. `_job_status()` derives status from directory + `results.rst` mtime — `log.txt` is *not* a readiness sentinel because it's created at submit time and grown during the run. `meta.json` (written at submit) holds the full parameter set; `_read_meta()` is called by `list_jobs`, `_finish` (for the log banner), and `_render_results` (for the .rst parameter section).
- **`RUNS_DIR` defaults to `gettempdir()/mock-gkn-hpc`.** Overridable via `--runs-dir` CLI flag or `MOCK_GKN_HPC_RUNS_DIR` env var. Set in `main()` before `mcp.run()` — the module-level `RUNS_DIR` is reassigned, so all tools/resources see the override.
- **Async submit, streaming log, terminal results.** `submit_ansys_run` returns immediately with a `job_id` and two `ansys://...` URIs. A background `asyncio.create_task(_finish(job_id))` walks `LOG_SCHEDULE` (a list of `(sleep, line)` tuples summing to `MOCK_RUN_SECONDS = 30s`) appending each line to `log.txt`, then renders `results.rst` from `src/mock_gkn_hpc/templates/results.rst.tmpl` with current date/time/job_id substituted in.
- **Resources are templated**: `ansys://{job_id}/log` and `ansys://{job_id}/results`. The log resource serves whatever has been written so far — clients can poll it to watch progress mid-run. The results resource 404s ("not found or still running") until `_finish` writes it at t≈30s. Both self-delete + 404 when past TTL.
- **`age_seconds` in `list_jobs` is intentionally inconsistent**: time-since-submit while pending (dir mtime), time-since-completion once ready (`results.rst` mtime). Not a bug — derives from the dir-vs-file state model.
- **Input pre-flight (`preflight.py`).** Before scheduling `_finish`, `submit_ansys_run` runs `check_deck()`: a small APDL walker (string/numeric parameters, comma-list arrays, `*DO` loops, `*CREATE` macros with `%argN%`, `%...%` substitution, `STRCAT`) that records every `/INPUT`/`CDREAD` file. Files must exist in the staged directory (input file's folder) or in the cluster file list (`cluster_files.txt`, `/project/...` paths; override with `MOCK_GKN_HPC_CLUSTER_FILES`). On a miss the job gets `error.txt` + an error log, `_job_status` reports `failed`, and the tool raises `ToolError` so the client sees the Ansys-style message. Unevaluable statements are skipped, never failed.
- `submit_ansys_run` mirrors GKN's `qansys` flags (`-i`, `-j`, `-o`, `--np`, `-p`, `-a`, `--highprio`). All are recorded in `meta.json` and surfaced in the log banner / results, but **none actually affect simulation behavior** — `-a` (start_time) is format-validated but doesn't delay the run; `--np` doesn't scale `MOCK_RUN_SECONDS`; `-p` is a `Literal["ansys", "meba", "mechs"]` enforced by Pydantic. The schema exists for client realism, not enacted semantics.

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
