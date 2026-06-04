# llm-orchestrator

Provider-agnostic LLM factory for the platform. Production
defaults to a configured real provider; synthetic providers are reserved
for isolated tests.

The package provides the LLM provider abstraction and is consumed
by every service/worker that needs to call an LLM (`automation-service`,
`assistant-service`, `admin-dashboard-api`, `agent-runner-worker`).

## Public API

```python
from llm_orchestrator import LLMProviderFactory

# Build whichever provider LLM_PROVIDER selects (default: "openai")
provider = LLMProviderFactory.from_env()
print(provider.complete("hello"))
```

The factory dispatches on the `LLM_PROVIDER` env var:

| `LLM_PROVIDER` | Class             | Status                          |
| -------------- | ----------------- | ------------------------------- |
| `openai`       | `OpenAIProvider`  | OpenAI Chat Completions API |
| `anthropic`    | `AnthropicProvider` | Anthropic Messages API |
| `vllm`         | `VLLMProvider`    | OpenAI-compatible vLLM endpoint |


## Standalone build & run

This is a Python library, not a service, but it is shipped as its own
package so each consuming service can pull it in via
`pyproject.toml`/`uv` without coupling to the rest of the workspace.

```bash
# From the workspace root, install the package into a fresh venv
python -m venv .venv
.venv/Scripts/activate            # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e libs/llm-orchestrator

# Smoke-test the configured provider
python -c "from llm_orchestrator import LLMProviderFactory; print(LLMProviderFactory.from_env().complete('hello'))"
```

## Configuration

| Env var               | Default      | Notes                                                            |
| --------------------- | ------------ | ---------------------------------------------------------------- |
| `LLM_PROVIDER`        | `openai`     | One of `openai` / `anthropic` / `vllm`                           |
| `LLM_MODEL_NAME`      | `gpt-5.5`    | Passed through to the provider constructor                       |
| `LLM_REASONING_EFFORT`| (blank)      | `minimal`/`low`/`medium`/`high`; sent only for reasoning models  |
| `LLM_VERBOSITY`       | (blank)      | `low`/`medium`/`high`; sent only for the gpt-5 family            |

Providers read credentials from environment or Vault-resolved runtime
configuration and fail closed when required credentials are missing.
The tuning knobs (`LLM_REASONING_EFFORT` / `LLM_VERBOSITY`) are applied
only when the configured model advertises support; otherwise they are
dropped so a non-reasoning model is not rejected by the upstream.
