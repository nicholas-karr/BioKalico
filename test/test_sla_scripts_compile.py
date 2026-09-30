"""Check that every Python script under scripts/sla/ compiles.

These run directly as services, so a syntax error would otherwise only show
up on the printer.
"""

import pathlib
import py_compile

import pytest

SLA_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sla"


def _sla_scripts():
    return sorted(SLA_DIR.glob("*.py"))


@pytest.mark.parametrize("path", _sla_scripts(), ids=lambda p: p.name)
def test_sla_script_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / "out.pyc"), doraise=True)
