# auth-shared

Shared OIDC/JWT validation primitives consumed by the HTTP services and
Temporal workers in this monorepo. The package ships an `OIDCValidator`
that performs JWKS-backed RS256 signature verification plus
`iss`/`aud`/`exp` claim checks in production, and a development bypass
(`auth_mode="dev"`) that returns canned admin claims for any non-empty
token so local workflows do not need a real IdP.

## Standalone build & run

This package is a regular [hatch](https://hatch.pypa.io/) / PEP 621
project and can be built and exercised on its own:

```bash
# from the repository root
cd libs/auth-shared

# create an isolated environment and install in editable mode
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

# import smoke-test
python -c "from auth_shared import OIDCValidator, OIDCConfig; print(OIDCValidator)"

# build a wheel (optional)
pip install build
python -m build
```

Runtime dependencies:

- `python-jose[cryptography]>=3.3,<4` — RS256 signature verification.
- `httpx>=0.27,<1` — JWKS document fetch.
