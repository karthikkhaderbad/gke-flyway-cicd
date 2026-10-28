import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture
def db_up(monkeypatch):
    monkeypatch.setattr(db, "db_version", lambda: "PostgreSQL 16.0")


@pytest.fixture
def db_down(monkeypatch):
    def boom():
        raise ConnectionError("no db")

    monkeypatch.setattr(db, "db_version", boom)


def test_root(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v42")
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"service": "gke-flyway-cicd", "version": "v42"}


def test_healthz_does_not_need_db(db_down):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok(db_up):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readyz_503_when_db_down(db_down):
    resp = client.get("/readyz")
    assert resp.status_code == 503


def test_db_health_ok(db_up):
    resp = client.get("/db/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "PostgreSQL 16.0"}


def test_db_health_503_when_db_down(db_down):
    resp = client.get("/db/health")
    assert resp.status_code == 503


def test_conninfo_reads_env(monkeypatch):
    monkeypatch.setenv("DB_HOST", "pg.example")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_NAME", "demo")
    monkeypatch.setenv("DB_USER", "u")
    monkeypatch.setenv("DB_PASSWORD", "p")
    info = db._conninfo()
    assert "host=pg.example" in info
    assert "port=6543" in info
    assert "dbname=demo" in info
    assert "user=u" in info
    assert "password=p" in info
