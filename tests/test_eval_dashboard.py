# -*- coding: utf-8 -*-
"""
tests/test_eval_dashboard.py — Tests for /eval-dashboard Flask app.
"""

import pytest
from eval_dashboard.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_eval_dashboard_healthz(client):
    """Test healthz endpoint returns 200 OK."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.data.strip() == b"ok"

    resp2 = client.get("/eval-dashboard/healthz")
    assert resp2.status_code == 200
    assert resp2.data.strip() == b"ok"


def test_eval_dashboard_index(client):
    """Test dashboard index page renders successfully."""
    resp = client.get("/eval-dashboard")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "SewaSetu Assistant Eval Dashboard" in html
    assert "Headline Performance" in html
    assert "Coverage Matrix" in html
    assert "Deterministic Security Suite" in html


def test_eval_dashboard_api_runs(client):
    """Test /api/runs returns JSON list."""
    resp = client.get("/eval-dashboard/api/runs")
    assert resp.status_code == 200
    assert resp.is_json
    data = resp.get_json()
    assert isinstance(data, list)


def test_eval_dashboard_assets(client):
    """Test serving persona test assets."""
    resp = client.get("/eval-dashboard/assets/c01_kamala_voter_id.jpg")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/jpeg")
