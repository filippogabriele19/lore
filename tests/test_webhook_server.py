import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from pathlib import Path

from cli.webhook_server import app

client = TestClient(app)

def test_github_webhook_ignored():
    # Only closed and merged PRs are processed
    payload = {
        "action": "opened",
        "pull_request": {
            "title": "Fix yaml parser",
            "body": "Use safe_load",
            "merged": False
        }
    }
    response = client.post("/webhooks/github", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

@patch("cli.webhook_server._extract_and_register_rules")
def test_github_webhook_processed(mock_extract):
    payload = {
        "action": "closed",
        "pull_request": {
            "title": "Security ADR for load",
            "body": "Constraint: yaml_load must use safe_load",
            "merged": True
        }
    }
    response = client.post("/webhooks/github", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    # Verify background task was scheduled
    mock_extract.assert_called_once()

def test_slack_webhook_verification():
    payload = {
        "type": "url_verification",
        "challenge": "test_challenge_123"
    }
    response = client.post("/webhooks/slack", json=payload)
    assert response.status_code == 200
    assert response.json()["challenge"] == "test_challenge_123"

@patch("cli.webhook_server._extract_and_register_rules")
def test_slack_message_processed(mock_extract):
    payload = {
        "event": {
            "type": "message",
            "text": "New rule: all endpoints must be authenticated",
            "user": "U12345"
        }
    }
    response = client.post("/webhooks/slack", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    # Verify background task was scheduled
    mock_extract.assert_called_once()
