import pytest
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.llm_client import load_local_env, get_llm_client, setup_llm_interactively, ensure_llm_configured

def test_load_local_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_API_KEY=my-secret-key-123\n# Comment\nANOTHER_KEY=\"val\"", encoding="utf-8")
    
    with patch.dict("os.environ", {}):
        load_local_env(tmp_path)
        assert os.environ.get("TEST_API_KEY") == "my-secret-key-123"
        assert os.environ.get("ANOTHER_KEY") == "val"

def test_config_load_and_provider_routing(tmp_path):
    # Setup lore config
    lore_dir = tmp_path / ".lore"
    lore_dir.mkdir()
    config_file = lore_dir / "lore.config.json"
    
    # 1. Test Anthropic routing
    config_data = {
        "llm": {
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest"
        }
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")
    
    with patch("anthropic.Anthropic") as mock_anthropic, patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-ant-key"}):
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Anthropic response")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 20
        mock_client.messages.create.return_value = mock_response
        
        client = get_llm_client(tmp_path)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": "hello"}]
        )
        
        assert resp.content[0].text == "Anthropic response"
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 20
        mock_client.messages.create.assert_called_once()

@patch("urllib.request.urlopen")
def test_openai_direct_requests_routing(mock_urlopen, tmp_path):
    # Setup lore config for OpenAI
    lore_dir = tmp_path / ".lore"
    lore_dir.mkdir(exist_ok=True)
    config_file = lore_dir / "lore.config.json"
    
    config_data = {
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1/chat/completions"
        }
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")
    
    # Mock urllib response
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "choices": [
            {
                "message": {
                    "content": "OpenAI response"
                }
            }
        ],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 25
        }
    }).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-openai-key"}):
        client = get_llm_client(tmp_path)
        resp = client.messages.create(
            model="gpt-4o",
            max_tokens=100,
            messages=[{"role": "user", "content": "hello"}]
        )
        
        assert resp.content[0].text == "OpenAI response"
        assert resp.usage.input_tokens == 15
        assert resp.usage.output_tokens == 25
        mock_urlopen.assert_called_once()

@patch("rich.prompt.Prompt.ask")
@patch("rich.prompt.Confirm.ask")
def test_interactive_setup_llm(mock_confirm, mock_prompt, tmp_path):
    # Setup mock answers:
    # 1. provider: openai
    # 2. model: gpt-4o
    # 3. custom URL: False
    # 4. API Key: key-xyz-123
    mock_prompt.side_effect = ["openai", "gpt-4o", "key-xyz-123"]
    mock_confirm.return_value = False
    
    config_data = {}
    updated_config = setup_llm_interactively(tmp_path, config_data)
    
    assert updated_config["llm"]["provider"] == "openai"
    assert updated_config["llm"]["model"] == "gpt-4o"
    assert updated_config["llm"]["base_url"] is None
    
    # Check .env file was created
    env_file = tmp_path / ".env"
    assert env_file.exists()
    env_content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=key-xyz-123" in env_content
    
    # Check gitignore updated
    gitignore_file = tmp_path / ".gitignore"
    assert gitignore_file.exists()
    assert ".env" in gitignore_file.read_text(encoding="utf-8")

@patch("urllib.request.urlopen")
def test_openai_tool_calls_routing(mock_urlopen, tmp_path):
    # Setup lore config for OpenAI
    lore_dir = tmp_path / ".lore"
    lore_dir.mkdir(exist_ok=True)
    config_file = lore_dir / "lore.config.json"
    
    config_data = {
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1/chat/completions"
        }
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")
    
    # Mock urllib response returning tool call
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "Using a tool",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "test_tool",
                                "arguments": "{\"arg1\": \"val1\"}"
                            }
                        }
                    ]
                }
            }
        ],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 25
        }
    }).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-openai-key"}):
        client = get_llm_client(tmp_path)
        resp = client.messages.create(
            model="gpt-4o",
            max_tokens=100,
            messages=[{"role": "user", "content": "hello"}],
            tools=[{
                "name": "test_tool",
                "description": "A test tool",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "arg1": {"type": "string"}
                    },
                    "required": ["arg1"]
                }
            }]
        )
        
        assert resp.stop_reason == "tool_use"
        assert len(resp.content) == 2
        assert resp.content[0].type == "text"
        assert resp.content[0].text == "Using a tool"
        assert resp.content[1].type == "tool_use"
        assert resp.content[1].id == "call_123"
        assert resp.content[1].name == "test_tool"
        assert resp.content[1].input == {"arg1": "val1"}
