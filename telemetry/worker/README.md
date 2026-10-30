# muse-discord usage stats Worker

A Cloudflare Worker with a D1 database that receives the anonymous
daily reports `telemetry.py` sends (its `build_payload` makes every
field). Only the maintainer deploys it.

| Route | What it does |
| --- | --- |
| `POST /v1/report` | One install's report for one UTC day, validated strictly (`src/validate.js`: known fields only, types, ranges, at most 4 KB) and stored by `(install_id, day)`, so a resent day replaces its row. At most 10 per install per day (then 429). Never stores the IP address, headers or request time. |
| `GET /v1/stats` | Public totals: installs active in the last 7 and 30 days, servers (each install's latest count, last 7 days), replies in the last 7 and 30 days. Cached for an hour. |
| `GET /v1/badge/servers` | A [shields.io endpoint](https://shields.io/badges/endpoint-badge) JSON: "used in N servers". |

Request logs are off in `wrangler.toml` (`[observability] enabled =
false`): they would hold IP addresses.

## Deploy

With Node 18 or newer and a free Cloudflare account, from
`telemetry/worker`:

```bash
npx wrangler login                     # opens a browser
npx wrangler d1 create muse-telemetry  # paste its database_id into wrangler.toml
npx wrangler d1 execute muse-telemetry --remote --file=schema.sql
npx wrangler deploy                    # prints the Worker's URL
curl https://muse-telemetry.<your-subdomain>.workers.dev/v1/stats
```

`TELEMETRY_URL` in `telemetry.py` holds that URL (no path, no trailing
slash). The `database_id` is not a secret. The README badge:

```markdown
![Used in](https://img.shields.io/endpoint?url=https%3A%2F%2Fmuse-telemetry.<your-subdomain>.workers.dev%2Fv1%2Fbadge%2Fservers)
```

## Changing it

Edit `src/`, run the tests (`node --test` here, plain Node), then
`npx wrangler deploy`. A change to the report's fields needs `SCHEMA`
raised in both `telemetry.py` and `src/validate.js`, and a new column
in `schema.sql`, applied with `npx wrangler d1 execute muse-telemetry
--remote --command "ALTER TABLE reports ADD COLUMN ..."` (a
database made before the `ahead` and `modified` columns needs both
added this way, `INTEGER NOT NULL DEFAULT 0`). The offline
suite `tests/telemetry` checks that what `telemetry.py` builds passes
`src/validate.js` (when `node` is installed).

## Looking at the data

Run any query with:

```bash
npx wrangler d1 execute muse-telemetry --remote --command "<SQL>"
```

Active installs per day:

```sql
SELECT day, COUNT(*) AS installs, SUM(servers) AS servers,
       SUM(replies_sent) AS replies
FROM reports GROUP BY day ORDER BY day DESC LIMIT 30;
```

Versions in use (last 7 days, each install's latest report):

```sql
SELECT version, COUNT(*) AS installs FROM (
  SELECT install_id, version, MAX(day) FROM reports
  WHERE day >= date('now', '-7 days') GROUP BY install_id)
GROUP BY version ORDER BY installs DESC;
```

Why messages went unanswered (last 30 days):

```sql
SELECT r.key AS reason, SUM(r.value) AS n
FROM reports, json_each(reports.refusals) AS r
WHERE day >= date('now', '-30 days')
GROUP BY reason ORDER BY n DESC;
```

Crashes by exception class and version:

```sql
SELECT version, c.key AS exception, SUM(c.value) AS n
FROM reports, json_each(reports.crashes) AS c
GROUP BY version, exception ORDER BY n DESC;
```

Reply latency (the average of the installs' daily p50 and p90, last 7
days):

```sql
SELECT day, COUNT(latency_p50) AS installs,
       AVG(latency_p50) AS avg_p50, AVG(latency_p90) AS avg_p90
FROM reports WHERE latency_p50 IS NOT NULL
  AND day >= date('now', '-7 days')
GROUP BY day ORDER BY day;
```

Feature flags (how many installs answer conversationally, keep memory
public-only, have media on, ...):

```sql
SELECT f.key AS flag, f.value AS value, COUNT(DISTINCT install_id) AS n
FROM reports, json_each(reports.features) AS f
WHERE day >= date('now', '-7 days')
GROUP BY flag, value ORDER BY flag, n DESC;
```

Presets and settings changes by where they were made:

```sql
SELECT 'preset' AS kind, p.key, SUM(p.value) FROM reports,
  json_each(reports.presets_applied) AS p GROUP BY p.key
UNION ALL
SELECT 'source', s.key, SUM(s.value) FROM reports,
  json_each(reports.settings_changes) AS s GROUP BY s.key;
```

To delete everything from one install (if its owner asks; they can
find their id with `bin/telemetry.py status`):

```sql
DELETE FROM reports WHERE install_id = '<id>';
```
