"""Unattended-worker false positives measured in a real run (Werkbank WB-793/WB-804).

Two scanner layers blocked routine commands of an unattended Hermes worker:

1. The dangerous-pattern detector read the ``--rm`` flag of ``docker run --rm`` /
   ``podman run --rm`` as the ``rm`` command (``\\brm`` matches after ``-``), so every
   throw-away container was flagged "recursive delete" / "delete in root path".
2. Tirith's runtime package threat-intel lookup (OSV / ecosyste.ms) can run out of its
   deadline. It then reports ``analysis_incomplete`` with evidence
   ``threat_type: lookup_incomplete`` and says itself that this "is incomplete
   verification, not evidence that the package is malicious". A warn made ONLY of such
   findings blocked every ``pip install`` of common packages (numpy) whenever the lookup
   was slow.

Both directions are pinned: the routine commands pass, real deletions and every other
Tirith finding keep blocking.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import tools.tirith_security as _tirith_mod
from tools.approval_detection import detect_dangerous_command
from tools.tirith_security import check_command_security


# --- 1. `--rm` is a flag, not the rm command ------------------------------------------------

@pytest.mark.parametrize("command", [
    "docker run --rm -v /mnt/models:/mnt/models --entrypoint ffprobe linuxserver/ffmpeg:latest -version",
    "docker run --rm --entrypoint tesseract jitesoft/tesseract-ocr --version",
    "podman run --rm -v /tmp:/tmp alpine true",
    "ssh spheron-AI-PC 'for t in 2 8; do docker run --rm -v /mnt/models:/mnt/models img -ss $t; done'",
    "docker compose run --rm web pytest",
])
def test_container_rm_flag_is_not_a_delete(command):
    is_dangerous, _key, description = detect_dangerous_command(command)
    assert not is_dangerous, description


@pytest.mark.parametrize("command", [
    "rm -rf /data/projects/agent_ticket",
    "rm /data/projects/agent_ticket -rf",
    "rm --recursive /data/projects/agent_ticket",
    "rm -rf build",
    "docker run --rm alpine true && rm -rf /data/projects/agent_ticket",
    "docker run --rm -v /data:/data alpine rm -rf /data/projects",
    "ssh spheron-AI-PC 'rm -rf /mnt/models/anarchy'",
])
def test_real_deletes_still_flagged(command):
    is_dangerous, _key, description = detect_dangerous_command(command)
    assert is_dangerous
    assert "delete" in description


# --- 2. A threat-intel lookup timeout alone is not a finding ---------------------------------

_CFG = {"tirith_enabled": True, "tirith_path": "tirith", "tirith_timeout": 5, "tirith_fail_open": True}

_LOOKUP_TIMEOUT = {
    "rule_id": "analysis_incomplete", "severity": "MEDIUM",
    "title": "Package threat intelligence could not be completed",
    "description": ("Tirith could not complete every configured runtime threat-intelligence check for "
                    "package 'numpy' (OSV lookup deadline exhausted). This is incomplete verification, "
                    "not evidence that the package is malicious."),
    "evidence": [{"confidence": "low", "source": "runtime-package-enrichment",
                  "threat_type": "lookup_incomplete", "type": "threat_intel"}],
}
_NESTED_UNRESOLVED = {
    "rule_id": "analysis_incomplete", "severity": "HIGH",
    "title": "Nested executable body could not be resolved", "evidence": [],
}
_UPLOAD_UNRESOLVED = {
    "rule_id": "analysis_incomplete", "severity": "HIGH",
    "title": "Could not resolve wrapped command for sensitive upload analysis",
    "evidence": [{"type": "text", "detail": "tirith:v1:data_flow;source=sensitive_asset;sink=remote_http"}],
}


@pytest.fixture(autouse=True)
def _tirith_resolved():
    _tirith_mod._resolved_path = "tirith"
    _tirith_mod._crash_count = 0
    _tirith_mod._circuit_open = False
    yield
    _tirith_mod._resolved_path = None


def _run(returncode, findings):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode, cp.stderr = returncode, ""
    cp.stdout = json.dumps({"findings": findings, "summary": "s"})
    return cp


@patch("tools.tirith_security.subprocess.run")
@patch("tools.tirith_security._load_security_config", return_value=_CFG)
def test_lookup_timeout_only_warn_is_allowed(_cfg, mock_run):
    mock_run.return_value = _run(2, [_LOOKUP_TIMEOUT, dict(_LOOKUP_TIMEOUT)])
    assert check_command_security("venv/bin/pip install -q numpy") == {
        "action": "allow", "findings": [], "summary": ""}


@pytest.mark.parametrize("returncode, findings", [
    (2, [_LOOKUP_TIMEOUT, {"rule_id": "threat_package_typosquat", "severity": "HIGH"}]),  # mixed
    (2, [_NESTED_UNRESOLVED]),                                     # other analysis gap
    (1, [_UPLOAD_UNRESOLVED]),                                      # block verdict
    (1, [_LOOKUP_TIMEOUT]),                                         # block is never downgraded
    (2, [dict(_LOOKUP_TIMEOUT, evidence=[])]),                      # no evidence -> no proof
    (2, [dict(_LOOKUP_TIMEOUT, evidence=_LOOKUP_TIMEOUT["evidence"]
              + [{"threat_type": "known_malicious"}])]),            # extra evidence
])
@patch("tools.tirith_security.subprocess.run")
@patch("tools.tirith_security._load_security_config", return_value=_CFG)
def test_other_findings_keep_their_verdict(_cfg, mock_run, returncode, findings):
    mock_run.return_value = _run(returncode, findings)
    result = check_command_security("venv/bin/pip install -q numpy")
    assert result["action"] == ("block" if returncode == 1 else "warn")
    assert result["findings"] == findings
