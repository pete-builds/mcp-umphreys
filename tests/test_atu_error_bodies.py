"""ATU error bodies are summarised, not pasted into a tool result.

The message reaches a tool result, which goes straight into an agent's context.
ATU's own JSON envelope carries a `message` worth keeping; an outage serves an
HTML page, and 300 characters of markup tells an agent nothing it can act on
while filling the context it was meant to inform.

Nothing here is a credential concern and these tests do not pretend otherwise:
ATU is a public API and this client sends no key at all.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_umphreys.clients.atu import ATUError, _describe_error_body


def _resp(status: int, body: str, content_type: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode(),
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://allthings.umphreys.com/api/v2/shows"),
    )


def test_the_json_message_is_kept():
    body = json.dumps({"error": True, "message": "Invalid date format"})
    assert _describe_error_body(_resp(400, body, "application/json")) == (
        ": Invalid date format"
    )


def test_an_html_outage_page_reports_its_shape_not_its_markup():
    html = "<html><head><title>502 Bad Gateway</title></head>" + "x" * 4000
    detail = _describe_error_body(_resp(502, html, "text/html"))

    assert "<html>" not in detail
    assert "text/html" in detail
    assert str(len(html)) in detail


def test_a_long_message_is_bounded():
    body = json.dumps({"message": "z" * 5000})
    assert len(_describe_error_body(_resp(400, body, "application/json"))) == 202


def test_an_empty_body_adds_nothing():
    """The raise site already renders the path and status."""
    assert _describe_error_body(_resp(500, "", "text/plain")) == ""


async def test_the_request_path_actually_uses_the_helper():
    """Guards the call site, not just the helper.

    Testing _describe_error_body alone would keep passing if someone put
    `resp.text[:300]` back at the raise site: the helper would still be correct
    and still be dead code.
    """
    from mcp_umphreys.clients.atu import ATUClient
    from mcp_umphreys.throttle import TokenBucket

    html = "<html><body>All Things Umphrey's is down</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=html.encode(),
                              headers={"content-type": "text/html"})

    client = ATUClient(throttle=TokenBucket(rps=100), base_url="https://atu.invalid/api/v2")
    await client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ATUError) as caught:
        await client._get("shows")

    assert "<html>" not in str(caught.value)
    assert "text/html" in str(caught.value)
    await client.aclose()
