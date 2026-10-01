"""Pre-flight check: the deck's /INPUT files must exist for the job."""

from pathlib import Path

from mock_gkn_hpc.preflight import check_deck

CLUSTER = {"/project/trs/model/ansys.inp"}

DECK = """\
*dim,path_load,string,128
*dim,file_load,string,128
n_lc=3
*dim,lc_id,array,n_lc
lc_id(1)=2,20,34 ! retained OEM cases
path_load(1)='{path_load}'
file_load(1)='limit_load_'
cdread,db,ansys,inp,/project/trs/model
*do,i,1,n_lc
    lcn=lc_id(i)
    /input,%file_load(1)%%lcn%,inp,%path_load(1)%
*enddo
"""

MACRO_DECK = """\
*create,run_case,mac
    /input,%arg1%,inp,limit_loads
*end
run_case,'limit_load_2'
run_case,'limit_load_20'
"""


def _stage(tmp_path: Path, deck: str) -> Path:
    loads = tmp_path / "limit_loads"
    loads.mkdir()
    for case in (2, 20, 34):
        (loads / f"limit_load_{case}.inp").write_text("/com\n")
    runscript = tmp_path / "runscript.ans"
    runscript.write_text(deck)
    return runscript


def test_staged_relative_path_passes(tmp_path):
    check = check_deck(_stage(tmp_path, DECK.format(path_load="limit_loads")), CLUSTER)
    assert check.ok
    assert len(check.files) == 4  # cdread + 3 load cases


def test_cluster_path_without_the_new_loads_fails(tmp_path):
    check = check_deck(_stage(tmp_path, DECK.format(path_load="/project/trs/loads")), CLUSTER)
    assert [Path(ref.path).name for ref in check.missing] == [
        "limit_load_2.inp", "limit_load_20.inp", "limit_load_34.inp",
    ]


def test_workstation_path_outside_staged_dir_fails(tmp_path):
    staged = tmp_path / "job"
    staged.mkdir()
    check = check_deck(_stage(staged, DECK.format(path_load=str(tmp_path / "elsewhere"))), CLUSTER)
    assert len(check.missing) == 3


def test_macro_calls_are_followed(tmp_path):
    check = check_deck(_stage(tmp_path, MACRO_DECK), CLUSTER)
    assert check.ok and len(check.files) == 2
