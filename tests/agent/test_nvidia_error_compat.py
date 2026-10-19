"""NVIDIA NIM error shapes measured live in WB-1215 (WB-1216).

* Unknown model on the hosted chat route → ``404`` with ``text/plain`` body
  ``404 page not found`` (no JSON). On the stock ``/v1`` route that can only mean
  "model not served": classify as ``model_not_found`` so the fallback chain takes
  over instead of three pointless retries, and say so in the one-liner.
* Bad key → ``401 application/problem+json`` ``{"status","title","detail"}``:
  the one-liner shows ``title — detail``, not the raw JSON.

Real ``openai`` exception objects built from fake ``httpx`` responses; no network.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from agent.api_error_summary import ApiErrorSummaryMixin
from agent.error_classifier import FailoverReason, classify_api_error

CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_V1 = "https://integrate.api.nvidia.com/v1"
UNKNOWN = "nvidia/does-not-exist-wb1215"


def _status_error(status: int, body: bytes, content_type: str, url: str = CHAT_URL):
    request = httpx.Request("POST", url)
    response = httpx.Response(status, content=body, headers={"content-type": content_type}, request=request)
    return openai.OpenAI(api_key="fake-key-not-real", base_url="http://unused.invalid")._make_status_error_from_response(response)


@pytest.fixture
def nim_404():
    return _status_error(404, b"404 page not found", "text/plain")


@pytest.fixture
def nim_401():
    return _status_error(
        401, b'{"status":401,"title":"Unauthorized","detail":"Invalid JWT serialization: Missing dot delimiter(s)"}',
        "application/problem+json")


class TestPlainText404:
    def test_hosted_route_unknown_model_is_model_not_found(self, nim_404):
        result = classify_api_error(nim_404, provider="nvidia", model=UNKNOWN, base_url=NIM_V1)
        assert result.reason == FailoverReason.model_not_found
        assert result.retryable is False
        assert result.should_fallback is True

    def test_hosted_route_via_custom_provider(self, nim_404):
        result = classify_api_error(nim_404, provider="custom", model=UNKNOWN, base_url=NIM_V1 + "/")
        assert result.reason == FailoverReason.model_not_found

    def test_wrong_base_path_stays_generic(self):
        """``…nvidia.com`` without ``/v1`` also 404s — that is a route problem, not a model one."""
        err = _status_error(404, b"404 page not found", "text/plain", url="https://integrate.api.nvidia.com/chat/completions")
        result = classify_api_error(err, provider="nvidia", model=UNKNOWN, base_url="https://integrate.api.nvidia.com")
        assert result.reason == FailoverReason.unknown

    def test_local_nim_stays_generic(self):
        err = _status_error(404, b"404 page not found", "text/plain", url="http://localhost:8000/v1/chat/completions")
        result = classify_api_error(err, provider="nvidia", model="my-local-nim", base_url="http://localhost:8000/v1")
        assert result.reason == FailoverReason.unknown

    def test_summary_names_the_model_cause(self, nim_404):
        line = ApiErrorSummaryMixin._summarize_api_error(nim_404)
        assert line.startswith("HTTP 404: 404 page not found")
        assert "model" in line.lower() and "NVIDIA" in line


class TestProblemJson401:
    def test_classified_as_auth(self, nim_401):
        result = classify_api_error(nim_401, provider="nvidia", model="nvidia/nemotron-3-super-120b-a12b", base_url=NIM_V1)
        assert result.reason == FailoverReason.auth

    def test_summary_is_title_and_detail_not_raw_json(self, nim_401):
        line = ApiErrorSummaryMixin._summarize_api_error(nim_401)
        assert line == "HTTP 401: Unauthorized — Invalid JWT serialization: Missing dot delimiter(s)"

    def test_detail_only_body(self):
        err = _status_error(403, b'{"detail":"Forbidden for this key"}', "application/json")
        assert ApiErrorSummaryMixin._summarize_api_error(err) == "HTTP 403: Forbidden for this key"
