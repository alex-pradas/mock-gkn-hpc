from fastmcp import FastMCP
from pydantic import Field, FilePath

mcp= FastMCP(
    "HPC at GKN, external demo",
    instructions="Simulates the HPC for Finite Elements analyisis at GKN"
    )


@mcp.tool
def submit_ansys_run(
    input_file: FilePath,
    version: str = "2025r1",
) -> str:
    """Submits run to HPC
    
    Returns success or failure.
    """

    return f"File {input_file} submitted to {version}." 

@mcp.tool
def status():
    return "online"

if __name__ == "__main__":
    mcp.run()
