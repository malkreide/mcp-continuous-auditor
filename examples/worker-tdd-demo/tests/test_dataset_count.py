"""TDD invariant in action: this async test is written and run RED *before* the
``dataset_count`` tool exists. Only once it fails locally is the tool implemented.

No live network — outbound httpx is replaced by an AsyncMock-style stub that
replays the recorded fixture (fastmcp-testing skill).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client

from zurich_opendata_demo.server import mcp

_FIXTURE = Path(__file__).parent / "fixtures" / "package_list.json"


class _FakeResponse:
    """Just enough of httpx.Response for the tool's happy path — no network."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self, **_: Any) -> Any:
        return self._payload

    def raise_for_status(self) -> _FakeResponse:
        return self


@pytest.mark.asyncio
async def test_dataset_count_counts_fixture_datasets() -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    expected = len(payload["result"])

    async def _fake_request(
        _self: Any, _method: str, _url: Any, *_a: Any, **_k: Any
    ) -> _FakeResponse:
        return _FakeResponse(payload)

    with patch("httpx.AsyncClient.request", new=_fake_request):
        async with Client(mcp) as client:
            result = await client.call_tool("dataset_count", {})

    data = json.loads(result.content[0].text)
    assert data == {"count": expected}
