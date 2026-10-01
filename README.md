# mock-gkn-hpc

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18836517.svg)](https://doi.org/10.5281/zenodo.18836517)

A public MCP **mock** of GKN's internal HPC server for Ansys / Finite Element analysis. It lets external clients exercise the full submit → poll → fetch-artifacts loop against a fake HPC — no real solver, queue, or cluster behind it.

This is a sample MCP server developed alongside the DUCTILE agentic orchestration paper. See the [DUCTILE repository](https://github.com/alex-pradas/DUCTILE) or the paper (DOI: TBD) for context.

## Install and run

```bash
uvx mock-gkn-hpc
```

Or wire it into your MCP client config:

```json
{
  "mcpServers": {
    "mock-gkn-hpc": {
      "command": "uvx",
      "args": ["mock-gkn-hpc"]
    }
  }
}
```

Job artifacts default to a system tempdir (`gettempdir()/mock-gkn-hpc`). Override with `--runs-dir /custom/path` or the `MOCK_GKN_HPC_RUNS_DIR` env var.

## Tool surface

`submit_ansys_run` mirrors GKN's `qansys` wrapper:

| Param | qansys flag | Default | Notes |
|---|---|---|---|
| `input_file` | `-i` | *(required)* | Path to `.ans` / `.cdb` |
| `version` | wrapper version | `"2025r1"` | Picks the `qansysX` release |
| `job_name` | `-j` | input filename stem | Descriptive only — storage uses a UUID |
| `output_file` | `-o` | auto | Recorded but not enacted by the mock |
| `np` | `--np` | `4` | Cosmetic; doesn't scale runtime |
| `product` | `-p` | `"ansys"` | One of `"ansys"`, `"meba"`, `"mechs"` |
| `start_time` | `-a` | `None` | `MMDDhhmm`; recorded but doesn't delay |
| `high_prio` | `--highprio` | `False` | Cosmetic |

All parameters round-trip through `meta.json`, the log banner, and the rendered `results.rst`.

## Input pre-flight check (v0.5.0)

A real solve stops at the first `/INPUT` or `CDREAD` whose file it cannot read. The mock has no
solver, so `submit_ansys_run` walks the runscript at submission (parameters, arrays, `*DO`
loops, macros and `%...%` substitution) and checks every file the deck would read. A job sees:

- its **staged directory**, the input file's folder (relative paths resolve against it), and
- the cluster's shared **`/project` storage**, described by `src/mock_gkn_hpc/cluster_files.txt`
  (override with the `MOCK_GKN_HPC_CLUSTER_FILES` env var, one absolute path per line).

If any file is missing, the job is recorded as `failed` (its log holds the error) and the tool
returns an MCP error with Ansys-style messages, e.g.

```
Job e816a8ac (runscript) failed during input processing: 6 file(s) read by the deck do not exist.
 *** ERROR ***
 /INPUT failed (runscript line 139). File /project/.../01_inputs/loads/limit_load_2.inp does not exist.
```

so an agent sees the failure in its context and can fix the deck and resubmit. A successful
submission reports `input_files_checked`. Statements the walker cannot evaluate are skipped,
not failed.

Each client can keep its own job store with `--runs-dir` (useful when several agents run in
parallel and should not see each other's jobs in `list_jobs`).

