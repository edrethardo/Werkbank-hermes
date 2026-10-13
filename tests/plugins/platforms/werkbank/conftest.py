"""Fixtures for the ``werkbank`` platform plugin tests (helpers live in ``werkbank_testkit``)."""

import pytest

from werkbank_testkit import FakeHost, running_server


@pytest.fixture
def fake_host():
    return FakeHost()


@pytest.fixture
def plugin_server(tmp_path, fake_host):
    """A running plugin server on a free port: ``.server .key .buffers .host .port``."""
    srv = running_server(tmp_path, fake_host)
    try:
        yield srv
    finally:
        srv.server.stop()
