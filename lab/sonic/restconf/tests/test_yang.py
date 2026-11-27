"""Le module sonic-lab.yang est valide (pyang --strict) et cohérent avec labmodule."""

import shutil
import subprocess
import sys

import pytest

from restconf import labmodule


def test_yang_module_is_valid():
    pyang = shutil.which("pyang")
    if pyang is None:
        pytest.skip("pyang absent")
    res = subprocess.run([pyang, "--strict", str(labmodule.YANG_FILE)], capture_output=True, text=True)
    assert res.returncode == 0 and not res.stderr.strip(), res.stderr


def test_yang_module_matches_labmodule():
    text = labmodule.YANG_FILE.read_text(encoding="utf-8")
    assert f'namespace "{labmodule.NAMESPACE}";' in text
    assert f"revision {labmodule.REVISION}" in text
    for leaf in labmodule.NEIGHBOR_LEAVES:
        assert f"leaf {leaf} " in text
    for op in labmodule.OPERATIONS:
        assert f"rpc {op.split(':')[1]} " in text
    assert sys.version_info >= (3, 11)
