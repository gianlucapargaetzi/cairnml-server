"""The access modules import each other in no particular order: each one must load first in a fresh interpreter."""

import subprocess
import sys

import pytest

MODULES = ("apiserver.bll.membership", "apiserver.bll.user_access", "apiserver.bll.access")


@pytest.mark.parametrize("first", MODULES)
def test_each_module_imports_first_in_a_fresh_interpreter(first):
    code = "; ".join(f"import {name}" for name in (first, *(m for m in MODULES if m != first)))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
