# Content policy

A per-account policy block in `config/{env}.json` sets what the server
will accept when drafts land in M2. M1 is read-only, so policy is only
loaded into the Lambda environment; nothing enforces it yet. See
[DESIGN.md section 7.5](../DESIGN.md) for the full spec.

## Fields

| Field | Meaning |
|---|---|
| `banned_characters` | List of forbidden Unicode code points (e.g. `"\u2014"` for em dash) |
| `banned_words` | Case-insensitive substring matches that fail validation |
| `required_signoff` | Literal that must appear at the end of the text (empty = no requirement) |
| `max_hashtags` | Integer cap; `-1` for no cap |
| `allowed_link_domains` | Non-empty list restricts link posts to those domains; empty permits any |
| `voice` | Documentation-only; surfaced in draft previews so the operator sees the intended register |

## Example (my accounts)

```json
"policy": {
  "banned_characters": ["\u2014"],
  "banned_words": ["Claude", "Anthropic"],
  "required_signoff": "",
  "max_hashtags": 5,
  "allowed_link_domains": ["justbardtech.com"],
  "voice": "brand or WILMA"
}
```

Policy violations block `draft_post` in M2 with a specific error per
field (not a generic "policy violation") so the model can self-correct.
Policy also runs again in the Publisher so a config change between
approval and publish is honored, regardless of the account's
`publish_mode`.
