"""The Windows installer (installer/install.ps1), read as text: there is no PowerShell test harness, so these check
the few lines whose regressions would matter most."""
from __future__ import annotations

import re
from pathlib import Path

INSTALLER = Path(__file__).parents[1] / "installer"


def _ps1() -> str:
    return (INSTALLER / "install.ps1").read_text(encoding="utf-8")


def test_winget_installs_python_from_its_own_source_and_unattended_with_yes():
    """Regression: without --source winget, a PC that had never used winget first asked to accept the Microsoft
    Store source's terms, and -Yes had nothing to answer that (or a package's agreement) with."""
    ps1 = _ps1()
    args = re.search(r"\$wingetArgs = @\(([^)]*)\)", ps1).group(1)
    assert "'--source', 'winget'" in args and "'--scope', 'user'" in args
    assert "accept" not in args                                    # a person at the console sees any agreement
    unattended = ps1[ps1.index("if ($Yes) {", ps1.index("$wingetArgs = @(")):]
    unattended = unattended[:unattended.index("}")]
    assert "'--accept-source-agreements', '--accept-package-agreements'" in unattended


def test_a_long_install_path_suggests_a_folder_in_the_users_own_profile():
    """Regression: it suggested C:\\FOLIO-Exchange, which every user of the PC can change (check warns about it)."""
    ps1 = _ps1()
    warning = ps1[ps1.index("This folder path is long."):]
    warning = warning[:warning.index("\n")]
    assert "C:\\FOLIO-Exchange" not in warning and "your own profile" in warning
    assert "Join-Path $env:USERPROFILE 'FOLIO-Exchange'" in ps1
