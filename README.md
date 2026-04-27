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
