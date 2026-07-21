from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DATABASE = Path(__file__).parent / "test_business_ai.db"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE.as_posix()}"
os.environ["TOKEN_SECRET"] = "test-only-secret"
# Tests must remain deterministic and must never send demo fixtures to a configured
# local cloud provider loaded from the repository-root .env file.
os.environ["LLM_API_KEY"] = ""
os.environ["DIFY_API_URL"] = ""
os.environ["DIFY_API_KEY"] = ""
os.environ["REDIS_URL"] = ""

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.answer_cache import final_answer_cache  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_final_answer_cache():
    final_answer_cache.clear()
    yield
    final_answer_cache.clear()


@pytest.fixture()
def client() -> TestClient:
    final_answer_cache.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=engine)
    # SQLite's QueuePool keeps an idle file handle on Windows until it is disposed.
    engine.dispose()
    if TEST_DATABASE.exists():
        TEST_DATABASE.unlink()
    final_answer_cache.clear()


def login(client: TestClient, email: str = "enterprise@neusoft.local") -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "Demo123!"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
