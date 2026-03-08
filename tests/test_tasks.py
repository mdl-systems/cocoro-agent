"""cocoro-agent — Tests: Tasks API"""
import pytest
import pytest_asyncio
import uuid
import json
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

# テスト用環境変数を先に設定
import os
os.environ.setdefault("COCORO_API_KEY", "test-key-123")
os.environ.setdefault("DATABASE_URL", "postgresql://cocoro:cocoro_secret@localhost:5432/cocoro_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from api.server import app


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def task_id():
    return str(uuid.uuid4())


@pytest.fixture
def sample_task(task_id):
    from datetime import datetime, timezone
    return {
        "id": task_id,
        "title": "AIトレンドリサーチ",
        "description": "2026年のAIトレンドを調査",
        "agent_type": "researcher",
        "priority": 5,
        "status": "queued",
        "progress": 0,
        "current_step": None,
        "result": None,
        "error": None,
        "tools_used": [],
        "webhook_url": None,
        "duration_seconds": None,
        "completed_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


@pytest.fixture
def mock_runner(task_id, sample_task):
    runner = MagicMock()
    runner.route_task = MagicMock(return_value="researcher")
    runner.submit_task = AsyncMock(return_value={"task_id": task_id, "status": "queued"})
    runner.get_task = AsyncMock(return_value=sample_task)
    runner.list_tasks = AsyncMock(return_value=([sample_task], 1))
    runner.redis_url = "redis://localhost:6379/0"
    return runner


@pytest.fixture
def mock_proxy():
    proxy = MagicMock()
    proxy.list_agents = AsyncMock(return_value=[{
        "id": "researcher",
        "name": "Research Agent",
        "department": "research",
        "status": "idle",
        "currentTask": None,
        "completedTasks": 42,
        "failedTasks": 0,
        "avgResponseTimeMs": 1200,
        "personality": {"traits": ["analytical"], "emotion": {"dominant": "trust"}},
        "lastActiveAt": None,
    }])
    proxy.get_org_status = AsyncMock(return_value={
        "departments": {"research": {"agents": 1, "activeTasks": 0}},
        "totalTasks": {"queued": 0, "running": 0, "completed": 10, "failed": 0},
    })
    return proxy


@pytest.fixture
def mock_webhook_sender():
    sender = MagicMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest_asyncio.fixture
async def client(mock_runner, mock_proxy, mock_webhook_sender):
    app.state.task_runner    = mock_runner
    app.state.agent_proxy    = mock_proxy
    app.state.webhook_sender = mock_webhook_sender
    app.state.db             = MagicMock()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key-123"},
    ) as c:
        yield c


# ── Tests ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "cocoro-agent"


@pytest.mark.asyncio
async def test_create_task(client, mock_runner, task_id):
    mock_runner.get_task.return_value["id"] = task_id
    resp = await client.post("/tasks", json={
        "title": "AIトレンドをリサーチして",
        "description": "2026年の主要AIトレンドを3つにまとめて",
        "type": "research",
        "priority": "normal",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "task_id" in data
    assert data["status"] == "queued"
    mock_runner.submit_task.assert_called_once()


@pytest.mark.asyncio
async def test_create_task_unauthorized():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        resp = await c.post("/tasks", json={"title": "test", "type": "research"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_task(client, mock_runner, task_id):
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert data["status"] == "queued"


@pytest.mark.asyncio
async def test_get_task_not_found(client, mock_runner):
    mock_runner.get_task.return_value = None
    resp = await client.get(f"/tasks/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_tasks(client, mock_runner):
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert "tasks" in data
    assert "total" in data
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_list_tasks_with_status_filter(client, mock_runner):
    resp = await client.get("/tasks?status=running")
    assert resp.status_code == 200
    mock_runner.list_tasks.assert_called_with(status="running", limit=20, offset=0)


@pytest.mark.asyncio
async def test_get_task_result_not_ready(client, mock_runner, task_id):
    # タスクがまだrunning中
    mock_runner.get_task.return_value["status"] = "running"
    resp = await client.get(f"/tasks/{task_id}/result")
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_get_task_result_completed(client, mock_runner, task_id):
    from datetime import datetime, timezone
    result_data = {"summary": "2026 AI trends: ...", "details": "...", "sources": []}
    mock_runner.get_task.return_value.update({
        "status": "completed",
        "result": json.dumps(result_data, ensure_ascii=False),
        "duration_seconds": 28.5,
        "completed_at": datetime.now(timezone.utc),
    })
    resp = await client.get(f"/tasks/{task_id}/result")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["result"]["summary"] == result_data["summary"]


@pytest.mark.asyncio
async def test_list_agents(client, mock_proxy):
    resp = await client.get("/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert "agents" in data
    assert len(data["agents"]) >= 1
    assert data["agents"][0]["id"] == "researcher"


@pytest.mark.asyncio
async def test_org_status(client, mock_proxy):
    resp = await client.get("/org/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "departments" in data
    assert "totalTasks" in data


@pytest.mark.asyncio
async def test_webhook_test(client, mock_webhook_sender):
    resp = await client.post("/webhooks/test", json={
        "url": "https://httpbin.org/post",
        "event": "task.completed",
    })
    assert resp.status_code == 200
    mock_webhook_sender.send.assert_called_once()
