# messages

Shared i18n message catalogs (Turkish + English) and the
`messages.load(locale)` loader stub. Catalogs live alongside the
package source in `src/messages/<locale>/messages.json` and are
empty placeholders in the scaffold; real strings will be added when
the corresponding service features are implemented.

## Standalone build & run

```bash
# from the repository root
cd libs/messages

# create an isolated environment and install in editable mode
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

# import smoke-test
python -c "import messages; print(messages.load('tr')); print(messages.load('en'))"

# build a wheel (optional)
pip install build
python -m build
```

Catalog files:

- `src/messages/tr/messages.json` (Turkish)
- `src/messages/en/messages.json` (English)

Both are currently empty JSON objects (`{}`).
