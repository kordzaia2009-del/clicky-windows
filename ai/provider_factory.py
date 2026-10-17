"""
Which LLM provider to use — shared by CompanionManager._get_llm() and code
that runs without the full app (e.g. `python -m agent.planner`), so both
always pick the provider the same way.
"""

from __future__ import annotations

import subprocess
import time
import urllib.request

from ai.base_provider import BaseLLMProvider
from config import cfg


def ensure_ollama_running():
    """Start Ollama if it isn't already running. Waits up to 8 s for it to be ready."""
    url = "http://localhost:11434/api/tags"
    for _ in range(2):
        try:
            urllib.request.urlopen(url, timeout=2)
            return  # already up
        except Exception:
            pass

    # API down. If an ollama process already exists, don't spawn a second
    # `ollama serve` — duplicate instances fight over the port and wedge the
    # API entirely. Just wait for the existing one below.
    already_running = False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ollama.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        already_running = "ollama.exe" in out.lower()
    except Exception:
        pass

    if not already_running:
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except FileNotFoundError:
            return  # ollama not installed, provider will fail gracefully

    # Wait up to 8 s for the server to come up
    for _ in range(16):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            pass


def create_llm_provider(name: str | None = None) -> BaseLLMProvider:
    """A new provider instance for `name`, or for the active provider."""
    provider = name or cfg.llm_provider()
    if provider == "claude":
        from ai.claude_provider import ClaudeProvider
        return ClaudeProvider()
    if provider == "openai":
        from ai.openai_provider import OpenAIProvider
        return OpenAIProvider()
    if provider == "gemini":
        from ai.gemini_provider import GeminiProvider
        return GeminiProvider()
    if provider == "copilot":
        from ai.github_copilot_provider import GitHubCopilotProvider
        return GitHubCopilotProvider()
    if provider == "lmstudio":
        from ai.lmstudio_provider import LMStudioProvider
        return LMStudioProvider()
    ensure_ollama_running()
    from ai.ollama_provider import OllamaProvider
    return OllamaProvider()
