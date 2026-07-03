"""Smoke test to verify the test harness works."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_health_returns_200(client: AsyncClient) -> None:
    """Verifies that the health check endpoint returns 200."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
