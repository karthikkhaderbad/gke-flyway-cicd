import os

from fastapi import FastAPI, HTTPException

from app import db

app = FastAPI(title="gke-flyway-cicd demo")


@app.get("/")
def root() -> dict:
    return {
        "service": "gke-flyway-cicd",
        "version": os.getenv("APP_VERSION", "dev"),
    }


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up. Deliberately does not touch the DB."""
    return {"status": "ok"}


def _check_db() -> str:
    try:
        return db.db_version()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: the app can serve traffic, which requires the DB."""
    _check_db()
    return {"status": "ready"}


@app.get("/db/health")
def db_health() -> dict:
    return {"status": "ok", "version": _check_db()}
