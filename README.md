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

Other tools and resources:

| Name | What it does |
|---|---|
| `job_status(job_id, wait_s=0)` | Status of one job (pending/ready/failed/expired); with `wait_s`, waits up to that many seconds (max 120) for a pending job to finish |
| `get_job_log(job_id)`, `get_job_results(job_id)` | The log so far and, once ready, the results file (for clients that cannot read MCP resources) |
| `list_jobs()` | Every job with its status and age |
| `delete_results(job_id)` | Deletes one job's log, results and metadata |
| `ansys://{job_id}/log`, `/results`, `/meta` | The job's log (as written so far), results file and `meta.json` |

The log is built from the job: the requested version and product, every file the deck reads,
and one `LOAD STEP` line per `SOLVE` the deck executes, with its load file.

Jobs are stateless: a job's status, log and results follow from its submit time (pending for
~30 s, then ready, expired 1 h later), so they need no running process and survive a server
restart (e.g. when a client session ends and a new one starts).

## Input pre-flight check (v0.6.1)

A real solve stops at the first `/INPUT` or `CDREAD` whose file it cannot read. The mock has no
solver, so `submit_ansys_run` walks the runscript at submission (parameters, arrays, `*DO`
loops, macros and `%...%` substitution) and checks every file the deck would read. A job sees:

- its **staged directory**, the input file's folder (relative paths resolve against it; an
  absolute path to the submitting machine is not visible to the job, even inside that folder), and
- the cluster's shared **`/project` storage**, described by `src/mock_gkn_hpc/cluster_files.txt`
  (override with the `MOCK_GKN_HPC_CLUSTER_FILES` env var, one absolute path per line).

If any file is missing, the job is recorded as `failed` (its log holds the error) and the tool
returns `status: "failed"` with Ansys-style messages (a normal result, not an MCP error), e.g.

```
Job e816a8ac (runscript) failed during input processing: 6 file(s) read by the deck do not exist.
 *** ERROR ***
 /INPUT failed (runscript line 139). File /project/.../01_inputs/loads/limit_load_2.inp does not exist.
```

so an agent sees the failure in its context and can fix the deck and resubmit. A successful
submission reports `input_files_checked` (and `notes`, e.g. a character value cut to 32
characters).

The walker follows APDL's parameter rules:
- array parameters must be dimensioned with `*DIM` before values are assigned, unless an
  implied (colon) loop defines them (`a(1:3)=1,2,3`);
- `CHAR` array elements hold 8 characters and scalar character parameters 32;
- in a `STRING` array the first subscript is the **character position** and the next ones
  select the string: `name(1,j)` is the j-th string. Writing `name(2)='x'` into a one-column
  STRING array therefore writes from character 2 of the same string.

It also handles `*SET`, `NINT`, `INT`, `ABS`, `CHRVAL` and `STRCAT`. A `/INPUT` or `CDREAD`
whose file name cannot be resolved with this subset is reported as a deck error, not skipped.

Each client can keep its own job store with `--runs-dir` (useful when several agents run in
parallel and should not see each other's jobs in `list_jobs`).

