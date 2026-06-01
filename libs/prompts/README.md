# prompts

Hybrid file + DB prompt loader shared across the HTTP services and
Temporal workers. The scaffold ships only the `PromptLoader.load`
placeholder; the real implementation will look up the latest version
of a prompt in the `shared.prompts` Postgres table and fall back to
the on-disk template under `prompts/<name>.md`.

## Standalone build & run

```bash
# from the repository root
cd libs/prompts

# create an isolated environment and install in editable mode
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

# import smoke-test
python -c "from prompts import PromptLoader; print(repr(PromptLoader().load('task_analysis')))"

# build a wheel (optional)
pip install build
python -m build
```

Note: the package name is `prompts`; do not confuse it with the
top-level `prompts/` directory at the repository root, which holds
shared prompt template files (placeholder content for now).
