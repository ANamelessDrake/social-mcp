# Environment configuration

Copy `example.json` to `<env>.json` (for example `dev.json`) and fill in the
values. Real config files are gitignored because they carry your AWS account
id and email.

| Field | Meaning |
|---|---|
| `account` | AWS account id to deploy into (use a personal account; DESIGN.md section 17) |
| `region` | AWS region (default `us-east-1`) |
| `environment_name` / `environment_name_upper` | Environment label used in resource names (`dev` / `Dev`) |
| `project_name` / `project_name_upper` | Pinned to `socialmcp` / `SocialMcp`; both forms are spliced into resource and secret names |
| `alarm_email` | Destination for CloudWatch alarms |
| `publishing_enabled` | Global publish kill switch (default `false`; publishing lands in milestone 2) |
| `allowed_client_hostnames` | Optional source-IP allowlist: DNS names whose current IPs may call the server; empty list disables the gate |
| `accounts` | One entry per account the deployment serves |

### Per-account fields

| Field | Meaning |
|---|---|
| `alias` | Short identifier used in tool calls and DynamoDB keys |
| `provider` | `facebook` for a Page; `instagram` for a professional account (M4+) |
| `publish_enabled` | Per-account publish kill switch (default `false`); overrides `publish_mode` when off |
| `publish_mode` | `manual` (every post needs operator approval; default), `auto` (server records implicit approval at draft time), `auto_for` (middle option; see `auto_for_kinds`) |
| `auto_for_kinds` | List of post kinds auto-approved when `publish_mode` is `auto_for` (e.g. `["text", "link"]`) |
| `policy` | Content policy enforced at draft time and again at publish time (DESIGN.md section 7.5) |

### Per-account policy fields

| Field | Meaning |
|---|---|
| `banned_characters` | Forbidden Unicode code points (e.g. `"\u2014"` for em dash) |
| `banned_words` | Case-insensitive substring matches that fail validation |
| `required_signoff` | Literal that must appear at the end of the text (empty = no requirement) |
| `max_hashtags` | Integer cap; `-1` for no cap |
| `allowed_link_domains` | Non-empty list restricts link posts to these domains; empty permits any |
| `voice` | Documentation-only note surfaced in the preview |

Deploy with `./deployCDK.sh <env>`, which passes `--context env=<env>` to the
CDK app. After deploying, connect each account once with
`python scripts/admin.py connect --env <env> --alias <alias>`.
