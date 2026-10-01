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


# --- APDL parameter semantics (0.6.0) ------------------------------------------------------

TWO_D_STRING_DECK = """\
*dim,file_load,string,32,3
file_load(1,1)='limit_load_2'
file_load(1,2)='limit_load_20'
file_load(1,3)='limit_load_34'
*do,ls_run,1,3
    /input,%file_load(1,ls_run)%,inp,limit_loads
*enddo
"""


def test_two_d_string_array_reference_resolves(tmp_path):
    """%arr(1,i)% is the i-th string of a STRING array: valid APDL, not split on its comma."""
    check = check_deck(_stage(tmp_path, TWO_D_STRING_DECK), CLUSTER)
    assert check.ok, (check.errors, check.missing)
    assert sorted(Path(r.path).name for r in check.files) == [
        "limit_load_2.inp", "limit_load_20.inp", "limit_load_34.inp",
    ]


ONE_COLUMN_STRING_DECK = """\
*dim,file_load,string,128
file_load(1)='limit_load_2'
file_load(2)='limit_load_20'
*do,ls_run,1,2
    /input,%file_load(ls_run)%,inp,limit_loads
*enddo
"""


def test_one_column_string_index_is_a_character_position(tmp_path):
    """On a one-column STRING array, name(2)= writes from character 2 of the same string."""
    check = check_deck(_stage(tmp_path, ONE_COLUMN_STRING_DECK), CLUSTER)
    # The string becomes 'llimit_load_20': ls_run=1 reads it whole, ls_run=2 from character 2
    assert [Path(r.path).name for r in check.files] == ["llimit_load_20.inp", "limit_load_20.inp"]
    assert [Path(r.path).name for r in check.missing] == ["llimit_load_20.inp"]


def test_undimensioned_array_assignment_is_an_error(tmp_path):
    deck = "lc_id(1)=2,20,34\n/input,limit_load_%lc_id(1)%,inp,limit_loads\n"
    check = check_deck(_stage(tmp_path, deck), CLUSTER)
    assert not check.ok
    assert any("must be dimensioned" in msg for _, msg in check.errors)


def test_implied_colon_loop_defines_the_array(tmp_path):
    deck = (
        "lc_id(1:3)=2,20,34\n"
        "*do,i,1,3\n    /input,limit_load_%lc_id(i)%,inp,limit_loads\n*enddo\n"
    )
    check = check_deck(_stage(tmp_path, deck), CLUSTER)
    assert check.ok, (check.errors, check.missing)


def test_nint_and_char_array(tmp_path):
    deck = (
        "*dim,lc_nums,array,3\nlc_nums(1)=2.0,20.0,34.0\n"
        "*dim,stem,char,1\nstem(1)='limit_lo'\n"  # 8 characters fit a CHAR element
        "*do,i,1,3\n    lc=nint(lc_nums(i))\n    /input,limit_load_%lc%,inp,limit_loads\n*enddo\n"
    )
    check = check_deck(_stage(tmp_path, deck), CLUSTER)
    assert check.ok, (check.errors, check.missing)


def test_unresolvable_file_read_is_an_error(tmp_path):
    """A file read whose name cannot be resolved fails instead of being skipped."""
    deck = "*get,lc_num,parm,lc_numbers,dim,x\n/input,limit_load_%lc_num%,inp,limit_loads\n"
    check = check_deck(_stage(tmp_path, deck), CLUSTER)
    assert not check.ok
    assert any("cannot be resolved" in msg and "LC_NUM" in msg for _, msg in check.errors)


def test_long_scalar_string_is_cut_to_32_characters(tmp_path):
    deck = "path_load='limit_loads/../limit_loads/../limit_loads'\n/input,limit_load_2,inp,%path_load%\n"
    check = check_deck(_stage(tmp_path, deck), CLUSTER)
    assert check.notes and "32 characters" in check.notes[0][1]
    assert check.files[0].path.startswith("limit_loads/../limit_loads/../li")
