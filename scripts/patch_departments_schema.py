"""Targeted minimal patches to config/departments.schema.json.

Operational schema updates:
- Add $schema = JSON Schema 2020-12 URL
- Department id pattern -> kebab-case only: ^[a-z][a-z0-9-]{1,30}$
- bot already has minProperties: 1 + anyOf; description+context note added
- BotEntry preserves existing 'credential_ref' and adds
  'email' + 'api_token_ref' as alternate; anyOf enforces
  one of {credential_ref} or {email, api_token_ref}.

Idempotent: running twice is a no-op.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "departments.schema.json"
SCHEMA_URL = "https://json-schema.org/draft/2020-12/schema"
DEPT_ID_PATTERN = "^[a-z][a-z0-9-]{1,30}$"


def main() -> None:
    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    data = json.loads(raw, object_pairs_hook=OrderedDict)

    changed: list[str] = []

    # 1. Ensure $schema is set as the FIRST key.
    if data.get("$schema") != SCHEMA_URL:
        new_data = OrderedDict()
        new_data["$schema"] = SCHEMA_URL
        for k, v in data.items():
            if k == "$schema":
                continue
            new_data[k] = v
        data = new_data
        changed.append("set $schema -> JSON Schema 2020-12")

    # 2. Department id pattern: kebab-case, no underscore.
    dept = data["$defs"]["Department"]["properties"]["id"]
    if dept.get("pattern") != DEPT_ID_PATTERN:
        dept["pattern"] = DEPT_ID_PATTERN
        changed.append("dept.id pattern -> kebab-case (no underscore)")
    if "kebab" not in dept.get("description", "").lower():
        dept["description"] = (
            "Slug. Kebab-case. Lowercase, harf-rakam-tire (alt cizgi yasak)."
        )
        changed.append("dept.id description updated")

    # 3. bot block sanity check (must already have minProperties: 1 + anyOf).
    bot = data["$defs"]["Department"]["properties"]["bot"]
    assert bot.get("minProperties") == 1, "bot.minProperties must be 1"
    assert "anyOf" in bot and len(bot["anyOf"]) == 3, "bot.anyOf must enforce jira|bitbucket|confluence"
    if not bot.get("description"):
        bot["description"] = (
            "En az bir servis (jira/bitbucket/confluence) tanimlanmalidir. Bk. design sec 3.6."
        )
        changed.append("bot.description added")

    # 4. BotEntry: keep credential_ref, add email + api_token_ref as
    # an alternate. Use anyOf so consumers can choose either form.
    bot_entry = data["$defs"]["BotEntry"]
    props = bot_entry.setdefault("properties", OrderedDict())

    if "email" not in props:
        props["email"] = OrderedDict([
            ("type", ["string", "null"]),
            (
                "description",
                "Atlassian hesabinin email adresi (api_token_ref ile birlikte; alternatif: credential_ref).",
            ),
        ])
        changed.append("BotEntry.email added")

    if "api_token_ref" not in props:
        props["api_token_ref"] = OrderedDict([
            ("type", ["string", "null"]),
            ("pattern", "^vault:[a-zA-Z0-9/_-]+$"),
            (
                "description",
                "Atlassian API token icin Vault path (email ile birlikte; alternatif: credential_ref).",
            ),
        ])
        changed.append("BotEntry.api_token_ref added")

    # Replace 'required: [credential_ref]' with anyOf so either credential form is valid.
    desired_anyof = [
        {"required": ["credential_ref"]},
        {"required": ["email", "api_token_ref"]},
    ]
    if bot_entry.get("anyOf") != desired_anyof:
        bot_entry["anyOf"] = desired_anyof
        changed.append("BotEntry.anyOf set (credential_ref OR email+api_token_ref)")
    if "required" in bot_entry:
        # Drop hard 'required' so anyOf governs credential form.
        del bot_entry["required"]
        changed.append("BotEntry.required dropped (anyOf governs)")

    if not bot_entry.get("description"):
        bot_entry["description"] = (
            "Bot servis kimligi. Vault tabanli kimlik icin 'credential_ref' ya da "
            "inline kimlik icin 'email' + 'api_token_ref'. En az biri zorunludur."
        )
        changed.append("BotEntry.description added")

    if changed:
        SCHEMA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("Updated:")
        for c in changed:
            print(f"  - {c}")
    else:
        print("No changes (schema already up to date).")


if __name__ == "__main__":
    main()
