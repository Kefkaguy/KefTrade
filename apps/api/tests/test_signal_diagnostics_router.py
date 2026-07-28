"""The signal-diagnostics endpoint must enqueue and return immediately.

This is the router-level half of the 502 fix: the POST handler must never
call the measurement itself, only `enqueue_signal_diagnostics_job`.
"""

from fastapi.testclient import TestClient


def _client(monkeypatch):
    from app.db import get_connection
    from app.main import app

    app.dependency_overrides[get_connection] = lambda: None
    return TestClient(app)


def test_post_enqueues_and_never_calls_the_measurement(monkeypatch):
    called = []
    monkeypatch.setattr(
        "app.services.signal_diagnostics.enqueue_signal_diagnostics_job",
        lambda conn, **kwargs: {"id": 42, "status": "queued"},
    )
    monkeypatch.setattr(
        "app.services.signal_diagnostics.run_signal_diagnostics",
        lambda *a, **k: called.append(1),
    )
    client = _client(monkeypatch)

    response = client.post("/research/intraday/signal-diagnostics", params={"timeframe": "30m"})

    assert response.status_code == 200
    assert response.json() == {"job_id": 42, "status": "queued"}
    assert called == [], "the request handler must never run the measurement itself"


def test_get_job_returns_the_stored_status(monkeypatch):
    monkeypatch.setattr(
        "app.services.signal_diagnostics.get_signal_diagnostics_job",
        lambda conn, job_id: {"id": job_id, "status": "completed", "result": {"families_measured": 3}},
    )
    client = _client(monkeypatch)

    response = client.get("/research/intraday/signal-diagnostics/jobs/42")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_get_job_404s_when_unknown(monkeypatch):
    monkeypatch.setattr(
        "app.services.signal_diagnostics.get_signal_diagnostics_job", lambda conn, job_id: None
    )
    client = _client(monkeypatch)

    response = client.get("/research/intraday/signal-diagnostics/jobs/999")

    assert response.status_code == 404
