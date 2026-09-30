"""Opt-in real Docker proof; image must already exist and contain Python/Git/bash."""
import importlib.util
import os
from pathlib import Path

import pytest


def test_actual_strict_container_boundary(tmp_path):
    image = os.environ.get('HERMES_TEST_IMAGE')
    if not image:
        pytest.skip('set HERMES_TEST_IMAGE to a trusted local Python/Git/bash image')
    path = Path(__file__).resolve().parents[2] / 'scripts/verify_docker_strict.py'
    spec = importlib.util.spec_from_file_location('strict_acceptance_probe', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.verify(image, tmp_path / 'run')
    assert report['docker_socket'] == 'absent'
    assert report['network'] == 'none'
    assert report['read_file']['workspace'] == 'readable'
    assert report['limits']['memory.max'] != 'max'
