"""cocoro-agent — Tests: Agents & Org API"""
import pytest
import pytest_asyncio
import os
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("COCORO_API_KEY", "test-key-123")
os.environ.setdefault("DATABASE_URL", "postgresql://cocoro:cocoro_secret@localhost:5432/cocoro_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from api.server import app


_MOCK_AGENTS = [
    {
        "id": "dev",
        "name": "Dev Agent",
        "department": "dev",
        "status": "idle",
        "currentTask": None,
        "completedTasks": 15,
        "failedTasks": 1,
        "avgResponseTimeMs": 800,
        "personality": {"traits": ["analytical", "precise"], "emotion": {"dominant": "trust"}},
        "lastActiveAt": None,
    },
    {
        "id": "researcher",
        "name": "Research Agent",
        "department": "research",
        "status": "busy",
        "currentTask": "task-abc123",
        "completedTasks": 42,
        "failedTasks": 0,
        "avgResponseTimeMs": 1200,
        "personality": {"traits": ["thorough", "analytical"], "emotion": {"dominant": "trust"}},
        "lastActiveAt": None,
    },
]

_MOCK_ORG = {
    "departments": {
        "dev":       {"agents": 1, "activeTasks": 0},
        "sales":     {"agents": 1, "activeTasks": 0},
        "marketing": {"agents": 1, "activeTasks": 1},
        "research":  {"agents": 1, "activeTasks": 1},
    },
    "totalTasks": {
        "queued": 2, "running": 2, "completed": 128, "failed": 3,
    },
}


@pytest.fixture
def mock_proxy():
    proxy = MagicMock()
    proxy.list_agents = AsyncMock(return_value=_MOCK_AGENTS)
    proxy.get_agent = AsyncMock(side_effect=lambda aid: next(
        (a for a in _MOCK_AGENTS if a["id"] == aid), None
    ))
    proxy.get_org_status = AsyncMock(return_value=_MOCK_ORG)
    return proxy


@pytest_asyncio.fixture
async def client(mock_proxy):
    runner = MagicMock()
    runner.redis_url = "redis://localhost:6379/0"
    app.state.task_runner    = runner
    app.state.agent_proxy    = mock_proxy
    app.state.webhook_sender = MagicMock()
    app.state.db             = MagicMock()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key-123"},
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_list_agents_returns_all(client):
    resp = await client.get("/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    ids = {a["id"] for a in data["agents"]}
    assert "dev" in ids
    assert "researcher" in ids


@pytest.mark.asyncio
async def test_get_agent_found(client):
    resp = await client.get("/agents/researcher")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "researcher"
    assert data["status"] == "busy"
    assert data["completedTasks"] == 42


@pytest.mark.asyncio
async def test_get_agent_not_found(client, mock_proxy):
    mock_proxy.get_agent.return_value = None
    resp = await client.get("/agents/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_org_status_structure(client):
    resp = await client.get("/org/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "departments" in data
    assert "totalTasks" in data
    assert "research" in data["departments"]
    assert data["totalTasks"]["completed"] == 128


@pytest.mark.asyncio
async def test_agents_require_auth():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        resp = await c.get("/agents")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_org_requires_auth():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        resp = await c.get("/org/status")
    assert resp.status_code == 401
