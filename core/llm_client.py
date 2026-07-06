"""
core/llm_client.py — Unified LLM Client Wrapper for LORE.
Provides a client object mimicking the Anthropic SDK, allowing LORE to be completely
LLM-agnostic and support Anthropic, OpenAI, and local/custom endpoints (like Ollama or OpenRouter).
"""
from __future__ import annotations

import os
import json
import logging
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [2, 5, 10]  # seconds between retries
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


def load_local_env(project_root: Path) -> None:
    """Loads project-local .env key-value pairs into os.environ."""
    env_path = project_root / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if not (val.startswith('"') or val.startswith("'")):
                        if "#" in val:
                            val = val.split("#", 1)[0].strip()
                    else:
                        quote_char = val[0]
                        end_idx = val.find(quote_char, 1)
                        if end_idx != -1:
                            val = val[1:end_idx]
                        else:
                            val = val[1:]
                    os.environ[key] = val
        except Exception as e:
            logger.warning(f"Failed to read local .env: {e}")

def _get_api_key(name: str) -> Optional[str]:
    """Retrieves API Key from environment or securely from Keyring (Bug 20)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        import keyring
        return keyring.get_password("lore", name)
    except ImportError:
        pass
    return None


class LLMContentBlock:
    def __init__(self, text_or_type: str, text: str = None, id: str = None, name: str = None, input: dict = None):
        if text_or_type in ("text", "tool_use"):
            self.type = text_or_type
            self.text = text
            self.id = id
            self.name = name
            self.input = input
        else:
            self.type = "text"
            self.text = text_or_type
            self.id = None
            self.name = None
            self.input = None


class LLMUsage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class LLMResponse:
    def __init__(self, text: str | None = None, content: list[LLMContentBlock] | None = None, input_tokens: int = 0, output_tokens: int = 0, stop_reason: str = "end_turn"):
        if content is not None:
            self.content = content
        elif text is not None:
            self.content = [LLMContentBlock(text)]
        else:
            self.content = []
        self.usage = LLMUsage(input_tokens, output_tokens)
        self.stop_reason = stop_reason


class MessagesEndpoint:
    def __init__(self, client: LoreLLMClient):
        self.client = client

    def create(self, model: str, max_tokens: int, messages: list[dict], system: str | None = None, **kwargs) -> LLMResponse:
        return self.client._call(model, max_tokens, messages, system, **kwargs)


class LoreLLMClient:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.messages = MessagesEndpoint(self)
        load_local_env(project_root)
        self.config = self._load_config()

    def _load_config(self) -> dict:
        config_path = self.project_root / ".lore" / "lore.config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _call(self, model: str, max_tokens: int, messages: list[dict], system: str | None = None, **kwargs) -> LLMResponse:
        llm_config = self.config.get("llm", {})
        provider = llm_config.get("provider")
        configured_model = llm_config.get("model")
        base_url = llm_config.get("base_url")

        # Fallback based on env vars
        if not provider:
            if _get_api_key("OPENAI_API_KEY") and not _get_api_key("ANTHROPIC_API_KEY"):
                provider = "openai"
            else:
                provider = "anthropic"

        # "default" sentinel means: use whatever is in lore.config.json
        if model == "default":
            target_model = configured_model or "deepseek-chat"
        else:
            target_model = configured_model or model

        if provider == "anthropic":
            api_key = _get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                import sys
                if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
                    api_key = "mock_test_key"
                else:
                    raise ValueError("ANTHROPIC_API_KEY environment variable is not set")

            from anthropic import Anthropic
            # Map standard aliases dynamically (Bug 27)
            if "sonnet" in target_model.lower():
                target_model = "claude-3-5-sonnet-latest"
            elif "haiku" in target_model.lower():
                target_model = "claude-3-5-haiku-latest"

            client = Anthropic(api_key=api_key)
            response = client.messages.create(
                model=target_model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                **kwargs
            )
            text = "".join(b.text for b in response.content if hasattr(b, "text") and b.text)
            return LLMResponse(
                text=text,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens
            )

        elif provider in ("openai", "openrouter", "deepseek"):
            api_key = _get_api_key("OPENAI_API_KEY")
            if not api_key:
                api_key = _get_api_key("OPENROUTER_API_KEY")
            if not api_key:
                api_key = _get_api_key("DEEPSEEK_API_KEY")
            if not api_key:
                import sys
                if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
                    api_key = "mock_test_key"
                else:
                    raise ValueError("No API key set for the selected LLM provider (check OPENAI_API_KEY / OPENROUTER_API_KEY / DEEPSEEK_API_KEY)")

            # Map to standard models if using defaults
            if provider == "openai":
                url = base_url or "https://api.openai.com/v1/chat/completions"
                if "claude-sonnet" in target_model or "claude-3-5-sonnet" in target_model:
                    openai_model = "gpt-4o"
                elif "claude-haiku" in target_model:
                    openai_model = "gpt-4o-mini"
                else:
                    openai_model = target_model
            elif provider == "deepseek":
                url = base_url or "https://api.deepseek.com/v1/chat/completions"
                openai_model = configured_model or "deepseek-chat"
            else:  # openrouter
                url = base_url or "https://openrouter.ai/api/v1/chat/completions"
                openai_model = configured_model or "google/gemini-2.5-pro"

            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {api_key}"
            }
            if provider == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/filippogabriele19/ase"
                headers["X-Title"] = "LORE Agent"

            payload_messages = []
            if system:
                payload_messages.append({"role": "system", "content": system})
                
            for msg in messages:
                role = msg.get("role")
                content = msg.get("content")
                
                if role == "assistant" and isinstance(content, list):
                    openai_tool_calls = []
                    text_content = ""
                    for block in content:
                        b_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                        if b_type == "text":
                            text_content += block.get("text", "") if isinstance(block, dict) else block.text
                        elif b_type == "tool_use":
                            b_id = block.get("id") if isinstance(block, dict) else block.id
                            b_name = block.get("name") if isinstance(block, dict) else block.name
                            b_input = block.get("input") if isinstance(block, dict) else block.input
                            openai_tool_calls.append({
                                "id": b_id,
                                "type": "function",
                                "function": {
                                    "name": b_name,
                                    "arguments": json.dumps(b_input)
                                }
                            })
                    
                    msg_obj = {"role": "assistant"}
                    if text_content:
                        msg_obj["content"] = text_content
                    else:
                        msg_obj["content"] = None
                    if openai_tool_calls:
                        msg_obj["tool_calls"] = openai_tool_calls
                    payload_messages.append(msg_obj)
                    
                elif role == "user" and isinstance(content, list) and any((b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_result" for b in content):
                    for block in content:
                        b_id = block.get("tool_use_id") if isinstance(block, dict) else block.tool_use_id
                        b_content = block.get("content") if isinstance(block, dict) else block.content
                        payload_messages.append({
                            "role": "tool",
                            "tool_call_id": b_id,
                            "content": b_content
                        })
                else:
                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict):
                                text_parts.append(block.get("text", ""))
                            elif hasattr(block, "text"):
                                text_parts.append(block.text)
                            else:
                                text_parts.append(str(block))
                        content = "".join(text_parts)
                    payload_messages.append({"role": role, "content": content})

            # Convert tools to OpenAI format if present
            openai_tools = None
            if "tools" in kwargs:
                openai_tools = []
                for tool in kwargs["tools"]:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["input_schema"]
                        }
                    })

            data = {
                "model": openai_model,
                "messages": payload_messages,
                "max_tokens": max_tokens
            }
            if openai_tools:
                data["tools"] = openai_tools

            last_err = None
            for attempt in range(1 + len(_RETRY_DELAYS)):
                req = urllib.request.Request(
                    url,
                    data=json.dumps(data).encode("utf-8"),
                    headers=headers,
                    method="POST"
                )
                try:
                    with urllib.request.urlopen(req, timeout=180) as response:
                        res_body = response.read().decode("utf-8")
                        res_json = json.loads(res_body)
                        choice = res_json["choices"][0]
                        finish_reason = choice.get("finish_reason")
                        message = choice["message"]
                        text = message.get("content") or ""
                        
                        content_blocks = []
                        if text:
                            content_blocks.append(LLMContentBlock("text", text=text))
                            
                        openai_tool_calls = message.get("tool_calls", [])
                        for tc in openai_tool_calls:
                            try:
                                t_input = json.loads(tc["function"]["arguments"])
                            except Exception:
                                t_input = {}
                            content_blocks.append(LLMContentBlock(
                                "tool_use",
                                id=tc["id"],
                                name=tc["function"]["name"],
                                input=t_input
                            ))
                            
                        stop_reason = "end_turn"
                        if finish_reason == "tool_calls" or openai_tool_calls:
                            stop_reason = "tool_use"
                            
                        usage = res_json.get("usage", {})
                        return LLMResponse(
                            content=content_blocks,
                            input_tokens=usage.get("prompt_tokens", 0),
                            output_tokens=usage.get("completion_tokens", 0),
                            stop_reason=stop_reason
                        )
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code in _RETRYABLE_CODES and attempt < len(_RETRY_DELAYS):
                        delay = _RETRY_DELAYS[attempt]
                        logger.warning(f"HTTP {e.code} on attempt {attempt+1}, retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                    err_body = e.read().decode("utf-8")
                    raise ValueError(f"HTTP Error {e.code}: {err_body}")
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last_err = e
                    if attempt < len(_RETRY_DELAYS):
                        delay = _RETRY_DELAYS[attempt]
                        logger.warning(f"Network error on attempt {attempt+1}: {e}, retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                    raise ValueError(f"Network error after {attempt+1} attempts: {e}")
            raise ValueError(f"All retry attempts exhausted: {last_err}")

        elif provider == "ollama":
            url = base_url or "http://localhost:11434/api/chat"
            ollama_model = configured_model or "llama3"

            headers = {"Content-Type": "application/json; charset=utf-8"}
            payload_messages = []
            if system:
                payload_messages.append({"role": "system", "content": system})
            for msg in messages:
                role = msg.get("role")
                content = msg.get("content")
                if isinstance(content, list):
                    content = "".join(b.get("text", "") if isinstance(b, dict) else (b.text if hasattr(b, "text") else str(b)) for b in content)
                payload_messages.append({"role": role, "content": content})

            is_openai = "chat/completions" in url or "/v1" in url

            data = {
                "model": ollama_model,
                "messages": payload_messages,
            }

            if is_openai:
                data["max_tokens"] = max_tokens
                data["options"] = {"num_ctx": 16384}
            else:
                data["stream"] = False
                data["options"] = {
                    "num_ctx": 16384,
                    "temperature": 0.0
                }

            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers=headers,
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as response:
                    res_body = response.read().decode("utf-8")
                    res_json = json.loads(res_body)
                    
                    if is_openai:
                        text = res_json["choices"][0]["message"]["content"]
                        usage = res_json.get("usage", {})
                        input_tokens = usage.get("prompt_tokens", 0)
                        output_tokens = usage.get("completion_tokens", 0)
                    else:
                        text = res_json["message"]["content"]
                        input_tokens = res_json.get("prompt_eval_count", 0)
                        output_tokens = res_json.get("eval_count", 0)
                        
                    return LLMResponse(
                        text=text,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens
                    )
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8")
                raise ValueError(f"Ollama HTTP Error {e.code}: {err_body}")

        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")


def setup_llm_interactively(project_root: Path, config_data: dict) -> dict:
    """Prompts the developer via rich CLI to set up their LLM configuration."""
    from rich.prompt import Prompt, Confirm
    from rich.console import Console
    
    console = Console()
    console.print("\n[bold magenta]⚙️  LLM Setup Helper (LORE Agnostic Engine)[/]")
    console.print("[dim]Configure LORE to use your preferred model provider.[/]\n")
    
    provider = Prompt.ask(
        "Select LLM provider",
        choices=["anthropic", "openai", "openrouter", "deepseek", "ollama"],
        default="anthropic"
    )
    
    default_models = {
        "anthropic": "claude-3-5-sonnet-latest",
        "openai": "gpt-4o",
        "openrouter": "google/gemini-2.5-pro",
        "deepseek": "deepseek-chat",
        "ollama": "llama3"
    }
    
    model = Prompt.ask(
        "Select model to use (press enter for default)",
        default=default_models[provider]
    )
    
    base_url = None
    if provider in ("ollama", "openai", "openrouter", "deepseek"):
        custom_url = Confirm.ask("Do you want to specify a custom API base URL?", default=False)
        if custom_url:
            base_url = Prompt.ask("Enter the custom API base URL")
            
    api_key = None
    if provider != "ollama":
        api_key = Prompt.ask(f"Enter API Key for {provider} (will be saved securely to .env)", password=True)
        
    config_data["llm"] = {
        "provider": provider,
        "model": model,
        "base_url": base_url
    }
    
    # Save key to .env
    if api_key:
        env_path = project_root / ".env"
        env_lines = []
        if env_path.exists():
            try:
                env_lines = env_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                pass
                
        env_key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        if provider == "deepseek":
            env_key = "DEEPSEEK_API_KEY"
            
        new_lines = []
        key_found = False
        for line in env_lines:
            if line.strip().startswith(env_key + "="):
                new_lines.append(f"{env_key}={api_key}")
                key_found = True
            else:
                new_lines.append(line)
        if not key_found:
            new_lines.append(f"{env_key}={api_key}")
            
        try:
            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            console.print(f"[bold green]✔ Saved API Key to local .env ({env_key})[/]")
        except Exception as e:
            console.print(f"[bold red]✖ Failed to save API Key to .env: {e}[/]")
            
        # Add to gitignore
        gitignore_path = project_root / ".gitignore"
        gitignore_lines = []
        if gitignore_path.exists():
            try:
                gitignore_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                pass
        if ".env" not in [line.strip() for line in gitignore_lines]:
            try:
                with open(gitignore_path, "a", encoding="utf-8") as gf:
                    gf.write("\n# LORE Local Env Key\n.env\n")
                console.print("[bold green]✔ Added .env to .gitignore[/]")
            except Exception:
                pass
                
    return config_data


def ensure_llm_configured(project_root: Path) -> None:
    """Checks if LLM is configured and prompts the user interactively if missing."""
    config_path = project_root / ".lore" / "lore.config.json"
    load_local_env(project_root)
    
    config_data = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass
            
    llm_config = config_data.get("llm", {})
    provider = llm_config.get("provider")
    
    # Check if we have the needed key
    has_key = False
    if provider == "ollama":
        has_key = True
    elif provider == "anthropic" or not provider:
        has_key = bool(_get_api_key("ANTHROPIC_API_KEY"))
    else:
        has_key = bool(_get_api_key("OPENAI_API_KEY") or _get_api_key("DEEPSEEK_API_KEY") or _get_api_key("OPENROUTER_API_KEY"))
        
    if not has_key:
        import sys
        if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
            return
        if sys.stdin.isatty():
            # Run interactive setup
            config_data = setup_llm_interactively(project_root, config_data)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to write config: {e}")
            load_local_env(project_root)
        else:
            raise ValueError(
                "LORE LLM provider is not configured or missing API key. "
                "Please run LORE in an interactive terminal to set it up, or set the env var (e.g. ANTHROPIC_API_KEY / OPENAI_API_KEY)."
            )


def get_llm_client(project_root: Path) -> LoreLLMClient:
    ensure_llm_configured(project_root)
    return LoreLLMClient(project_root)
