# pii-shared

Deterministic PII (Personally Identifiable Information) regex masker used by
`assistant-service` (and any other consumer that handles user-provided text
before it is forwarded to an LLM, audit log or downstream tool call).

The package is a *pure function* layer:

- No I/O, no logging, no global state.
- Same input always produces the same output (`mask(text)` is referentially
  transparent).
- No third-party runtime dependencies - only the Python standard library.

## Public API

```python
from pii_shared import mask, PiiMatch, PII_PATTERNS

masked, matches = mask("TC: 12345678901, mail: ali@example.com")
# masked  -> "TC: ***TC_REDACTED***, mail: ***EMAIL_REDACTED***"
# matches -> [PiiMatch(kind="tc_kimlik", start=4, end=15),
#             PiiMatch(kind="email",     start=23, end=38)]
```

## Patterns

| `kind`        | Pattern                                                         | Replacement                |
| ------------- | --------------------------------------------------------------- | -------------------------- |
| `tc_kimlik`   | `\b\d{11}\b` (11 numeric digits - pattern based, no checksum)   | `***TC_REDACTED***`        |
| `phone_tr`    | `\b5\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}\b`                      | `***PHONE_REDACTED***`     |
| `email`       | RFC 5322 lite `\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b` | `***EMAIL_REDACTED***`     |
| `credit_card` | `\b(?:\d[ -]?){13,19}\b` filtered by Luhn check                 | `***CC_REDACTED***`        |

For credit cards the regex matches any 13-19 digit run (with optional
single space/dash separators); a candidate is only redacted **and** added
to `matches` when `_luhn_valid(...)` returns `True`. Invalid Luhn
candidates are left untouched in `masked` and are not reported in
`matches`.

## Determinism guarantee

`mask(text)` is deterministic: for any string `text`, the returned
`(masked, matches)` tuple is byte-for-byte identical across processes,
runs and Python versions in `>=3.12,<3.13`. This is exercised by the
property test suite (`platform/tests/property/test_pii_filter.py`,
PII masking validation).

## Standalone build & run

```bash
cd platform/libs/pii-shared
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

python -c "from pii_shared import mask; print(mask('arayın 0532 123 45 67'))"
```

Runtime dependencies: none (standard library only).
