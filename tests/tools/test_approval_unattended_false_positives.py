"""Unattended-worker false positive measured in a real run (Werkbank WB-793).

The dangerous-pattern detector read the ``--rm`` flag of ``docker run --rm`` /
``podman run --rm`` as the ``rm`` command (``\\brm`` matches after ``-``), so every
throw-away container was flagged "recursive delete" / "delete in root path".

Both directions are pinned: the routine commands pass, real deletions keep blocking.
(The second half of this file covered the bundled tirith scanner, which upstream removed.)
"""
import pytest

from tools.approval_detection import detect_dangerous_command


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
