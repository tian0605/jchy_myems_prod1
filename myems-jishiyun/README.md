# Jishiyun acquisition service (in development)

This directory contains contract validation and an executable read-only HTTP
verification client and a separate queued-energy worker, each with its own
container definition. The worker connects explicit queue submissions and
**opt-in hourly scheduling** to HTTP and raw-result persistence. Explicit
historical requests are tracked as durable resumable batches. Transient transport
failures have bounded durable retries and credential-wide provider cooldowns.
Approved archive initialization has a transactional library and trusted local
commands. Provider archive fetch and approved selection execution are now queued
worker phases with persistent per-meter outcomes and summary counts. The Admin
data-source page now has authenticated monitoring/control and archive-fetch,
preview, batch-approval and initialization-submission operations. A separate
audited raw-mapping enable/pause control is implemented. An explicit, first-write
`business-hour` acceptance command and an opt-in durable first-write scheduler
are available; multi-hour business processing, historical correction and
downstream recomputation remain subsequent stages. Isolated
local container acceptance of the implemented acquisition slice passed on
2026-10-15 using MySQL 8.4 and a synthetic HTTPS provider. Bounded real-provider
raw collection and first-write hourly acceptance followed on 2026-10-16 as
described below; full continuous business acceptance remains pending.
The verification CLI remains read-only and never invokes persistence.
Provider-confirmed midnight convention (2026-10-17): an electric-quantity
`ReadingDate` at local `23:59:00` labels the **next day's 00:00 cumulative
register**; the provider does not return a separate 00:00 reading. Only that
exact time is normalized before coverage, deduplication and hourly processing.
The original `ReadingDate` remains unchanged in the raw JSON. Archive dates
and query windows are not shifted; other off-grid times still fail validation,
and conflicting readings at the normalized business key remain invalid.
Do not start it in production or enable mapped points in the existing normalization
worker before the ownership and missing-data safeguards are implemented.

## Cross-day local acceptance and bounded test renewal (2026-10-17)

### Approved corrections and company/device dimensions (2026-10-17)

The trusted local operator CLI now supports **raw-revision correction**, not
rate-policy changes or arbitrary overwriting of historical results:

```powershell
docker exec myems-jishiyun-local-worker-1 python worker.py business-correct --run-id 31 --actor 1 --test-only
# Review affected hours, old/new registers and all downstream values; copy approval_hash.
docker exec myems-jishiyun-local-worker-1 python worker.py business-correct --run-id 31 --actor 1 --test-only --expected-hash "<reviewed-hash>" --apply
```

No change returns `no_changes`. Apply requires the exact current preview hash.
A shared boundary includes **both neighboring committed hours**, including across
midnight, and every overlapping run's audit proof. Missing/invalid samples,
non-increasing revisions, changed ownership or rates, unowned/duplicated/tampered
targets and unsupported dependencies block rather than partially repair data.
Current scope requires audited settlement for every affected hour. Original
first-write audits remain immutable; one append-only correction event preserves
old/new proofs and values, approving actor and hash. Cumulative business readings,
hourly energy, billing, carbon and any already-materialized supported dimension
hours update with that audit in one cross-schema InnoDB transaction. Stale
approvals and audit failures write nothing; a proven replay writes nothing.
Normal business processing uses the latest validated proof chain, not retired
first-write values. These are trusted-host commands, **not** an authenticated
Admin-page approval feature or an automatically approved correction queue.

Company space **2** now directly counts only the nine enabled meters; removing
the other nine **space associations** did not delete meters, mappings or history,
or enable paused acquisition. Equipment **1** and product **2** are explicitly
labelled `PILOT TEST Jishiyun source 4`. Equipment 1 belongs to space 2 for
navigation but `is_input_counted=0`, so its meters are not counted a second time
inside the space. The space's original uncounted-parent flags remain unchanged.
Both objects carry `JISHIYUN_PILOT_SOURCE:4` in `description` as the explicit
dedicated-writer ownership marker; do not remove it while that writer is enabled.
Their original configuration is recorded in `pilot_dimensions_configured`.

```powershell
docker exec myems-jishiyun-local-worker-1 python worker.py business-dimensions --actor 1 --start "2026-10-16T10:00:00+08:00" --end "2026-10-17T10:00:00+08:00" --apply
docker exec myems-jishiyun-local-worker-1 python /acceptance/dimension_reconcile.py
docker exec myems-jishiyun-local-worker-1 python /acceptance/correction_acceptance.py
```

The dimension command defaults to read-only preview and requires **every member
hour in all three meter tables**, never missing-as-zero. Existing sums require
their matching dimension proof. With the current opt-in pilot settlement
scheduler, new jobs aggregate configured dimensions after settlement; a
`dimensions:` gate is durable and retries, without claiming that earlier energy
or settlement transactions rolled back. Existing done jobs are not implicitly
rescanned; the explicit command filled the historical acceptance window.

The 24-hour real-company test passed 144 stored dimension-hour checks, 12
authenticated report responses and six real XLSX exports. Space and equipment
each match 850.730000 kWh, 1956.679000 test CNY and 1276.095000 test kgCO2e.
Space product reports currently sum the product's direct meter rows, while
equipment reports read equipment category aggregates; both were independently
checked against the materialized tables. Direct staged-space reports bypass
Redis reads/writes, ordinary spaces keep their cache. No Redis flush was used.

The correction rehearsal copies the **same nine real pilot meters** into
randomly named temporary MySQL schemas, changes only the copy's midnight boundary
by 0.25 kWh, and verifies adjacent hours, 12 dimension rows, stale approval
rejection, audit-failure rollback and two-connection concurrent replay. It deletes
only the schemas it created, and verifies the supplier's original record is
unchanged. The real run-31 preview is `no_changes`: there has not yet been a real
provider revision to approve. This does not claim live supplier correction
acceptance. Unsupported virtual/combined/shopfloor/store/tenant dependencies,
output/item dimension history or changed topology are explicit gates, not
silently left stale. Current sign-off is direct-input **category** dimensions;
arbitrary hierarchies, output/item rollups and tariff-change corrections are
not covered.

Run 31 passed the exact nine-meter window `[2026-10-16 10:00, 2026-10-17 10:00)`
UTC+8: 864 interval slots, 873 unique registers including the end boundary,
216 hourly energy rows and 216 rows in each test billing/carbon table.
Independent SQL, 27 authenticated reports and three labelled XLSX responses
agree: 850.730000 kWh, 1956.679000 test CNY and 1276.095000 test kgCO2e.
This proves a complete **recovered** day, not uninterrupted worker uptime.

An operator explicitly approved a second isolated test period,
`[2026-10-17 00:00, 2026-10-19 00:00)` UTC+8, at the same 2.3 CNY/kWh and
1.5 kgCO2e/kWh. The local configuration helper's `--new-period` option appends
a contiguous, non-overlapping, separately named tariff and audit; it does not
extend or overwrite the original tariff or change the carbon factor. It verifies
exclusive ownership by the approved nine meters. Preview is the default;
`--apply` is required. Replays write nothing. Expiry blocks settlement again,
without stopping raw collection or hourly energy processing.

The reusable read-only reconciliation command is:

```powershell
docker exec myems-jishiyun-local-worker-1 python /acceptance/pilot_reconcile.py --run-id 31 --start "2026-10-16T10:00:00+08:00" --end "2026-10-17T10:00:00+08:00"
```

The long-running worker now isolates a provider contract failure **only after**
the run failure and audit are durably recorded. It logs a fixed error code and
run ID, keeps both schedules and heartbeat active, and does not retry the failed
run automatically. `run --once` exits 2 for that failure, never success or idle.
Unrecorded contract errors, database/persistence errors and unexpected failures
still stop the worker with an explicit error. Raw response/exception text and
credentials are not logged. Approval-based historical correction and
space/equipment aggregation acceptance remain outstanding.

## Controlled business-hour acceptance (2026-10-16 historical baseline)

The limitations below describe the initial baseline; later multi-hour settlement,
correction and dimension capabilities are documented in the newer sections above.

This is a **trusted operator CLI**, not an HTTP authorization boundary or an
automatic normalization loop. Use an explicitly selected successful energy run
covering exactly one UTC-aligned hour. The immutable run scope must have four
valid expected slots per meter, no missing/invalid/unexpected counts, and each
mapping must still be ready/enabled. This does not promote the point type.

After confirming with the provider that `ReadingDate` is the timestamp's
cumulative kWh register, preview before committing:

```powershell
docker exec myems-jishiyun-local-worker-1 python worker.py business-hour --run-id 12 --actor 1 --confirm-timestamp-kwh
docker exec myems-jishiyun-local-worker-1 python worker.py business-hour --run-id 12 --actor 1 --confirm-timestamp-kwh --apply
```

Run/operator IDs above refer to the current **local** acceptance fixture, not
production defaults. Without the confirmation flag, the command exits `2`
with a named `blocked` gate. Without `--apply`, it does not write business data.
The insert counts in a preview are proposed counts, not committed counts.

Gates and transaction behavior:

- Require all five valid `mi15/SJZ` samples, including both hour boundaries;
  no interpolation, first-reading-as-consumption, sum of registers, or zero fill.
- Check exact external identity, exclusive meter/point ownership, staged
  `JISHIYUN_REGISTER` type, kWh, ratio 1, offset 0 and trend/non-virtual settings.
- Reject unresolved alerts in the hour or boundary, any decreasing adjacent
  register (including a reset followed by recovery), cumulative high-limit
  violations and hourly consumption outside configured limits. A constant
  valid register is a genuine zero-consumption hour.
- Require participating tables on the same MySQL server to use InnoDB.
  Reuse the acquisition source-row lock, check all devices before writing, and
  atomically insert historical registers, hourly energy and operator audit.
  Staged ownership excludes these points from the legacy normalization loop;
  do not change them to `ENERGY_VALUE` to bypass this ownership boundary.
- Check existing business keys under lock. Never append duplicate keys, adopt
  an unproven existing hour, or overwrite differing values. A second apply of
  the **same run** verifies its audit proof and persisted values and returns
  `already_committed` without more rows/events.
- Record the raw values, timestamps and revisions in `business_hour_committed`.
  Changed committed inputs require explicit historical recalculation rather
  than silently replacing the old result. A different run for an already
  committed hour is not a replay.
- On first write, refuse if billing or carbon has already advanced to/past this
  hour: inserting historical energy alone would not repair downstream results.
  A no-op proven replay remains allowed.

Scope is deliberately limited to **first-write acceptance**, not final business
activation. `run --schedule` only schedules acquisition; it never calls this
command. Multi-hour automatic business processing, durable correction/recompute
jobs, billing/carbon acceptance and cache invalidation are not implemented here.
Existing reports may cache old results for 30 minutes; a successful SQL write
does not prove cached pages refreshed. The staged point's legacy latest-value
and cumulative-curve UI remain inactive; hourly energy reports read the energy
table independently. Report display placeholders outside the accepted hour do
not prove additional hours were computed.

Current real-provider local acceptance: run 12 has nine meters / 36 valid slots
for `[10:00,11:00)` Beijing time on 2026-10-16. Run 14 fetched the nine 11:00
boundary samples using `[11:00,11:15)`. The confirmed formula produced 45
historical registers and nine hourly rows totaling **55.310000 kWh**, with one
actor-attributed audit. Two real connections concurrently applying run 12
returned `committed` and `already_committed`. A fault after 45 historical and
four hourly inserts rolled everything back before the successful commit.
Independent SQL differences and all nine existing hourly-report API totals
matched. This is not billing/carbon or continuous-business acceptance.

The local worker now runs with `--schedule`, and task 8 is enabled. Its first
automatic run 13 collected `[16:00,17:00)` Beijing time: 36 expected/valid,
zero missing/unexpected. Startup replays the latest eligible tick immediately;
it does not wait for the next `:40`. Keep the ignored local provider and
schedule overrides when restarting:

```powershell
docker compose --env-file '.\others\jishiyun-local\.env' --env-file '.\myems-jishiyun\.env' `
  -f '.\others\docker-compose-jishiyun-local.yml' `
  -f '.\others\jishiyun-local\state\compose.provider.yml' `
  -f '.\others\jishiyun-local\state\compose.schedule.yml' `
  up -d --no-deps --no-build worker
```

To disable scheduling, pause task 8 through the workbench; to remove scheduler
mode as well, omit the last override and force-recreate only the worker.
Keep the provider override in either case. No API key is printed or stored in
these command lines.

## Durable business scheduling (2026-10-16 increment)

Apply the additive extension script again before enabling this feature. It now
also creates `myems_system_db.tbl_jishiyun_business_jobs`, bringing the extension
to eleven InnoDB tables. Existing raw readings, runs and business data are not
deleted. `CREATE TABLE IF NOT EXISTS` is not a migration for incompatible table
definitions; inspect existing definitions before production upgrades.

Business execution is **off by default** and separate from raw `--schedule`:

```powershell
python .\worker.py run --schedule --business-schedule --business-actor 1 --confirm-timestamp-kwh --health-file "worker-health.json"
```

The actor is the real operator approving the loop, not an authentication
mechanism; this is a trusted deployment command. In the local fixture the
ignored `compose.schedule.yml` now contains those options with actor 1. To
pause only business work, remove the three business options from that override
and recreate the worker, retaining `--schedule` and provider configuration.
Pausing acquisition task 8 does **not** stop already discovered business jobs.
Omitting the entire schedule override disables both loops. No automatic
historical rewrite, billing or carbon loop is enabled by this option.

Each worker iteration discovers at most 100 successful energy runs without a
job, then claims at most one due job. Discovery and claiming use the shared
source lock, and run ID is unique in the job table. A five-minute lease makes
abandoned attempts recoverable. Completion checks the token and current lease.
The original approving actor is retained on retry. If energy commit succeeds
but job acknowledgement is interrupted, the next claim verifies the existing
proof and records completion with zero new rows rather than duplicating data.

Job status:

- `queued`: awaiting a claim.
- `running`: a leased business attempt, distinct from acquisition execution.
- `blocked`: an explicit normalization or pilot settlement gate failed;
  retry is due in five minutes. A normalization gate writes no energy rows;
  a `settlement:` gate may follow an energy commit but writes no settlement rows.
  Missing end boundaries may arrive in the next
  raw collection without an extra supplier call. The latest hour may therefore
  remain pending for an additional collection cycle.
- `done`: first-write commit or a verified no-op replay. Insert counts describe
  the most recent attempt, not total rows belonging to that run.
- `error`: an unexpected exception, stored only as `business_execution_failed`
  and propagated to the worker. Inspect logs and explicitly requeue; it is not
  automatically retried as success.

Current normalization supports successful **1-to-24-hour aligned runs**.
Every hour needs five valid quarter-hour registers including its end boundary.
All hours are validated and committed together; a shared boundary is inserted
only once. Auxiliary 15-minute boundary fetches remain blocked with
`aligned_whole_hour_run_up_to_one_day_required`. Overlapping runs reuse an hour
only when its complete audited proof, raw revisions and stored business values
match; unowned or changed rows are never adopted. Version-1 single-hour proofs
remain replay-compatible; version-2 proofs contain the accepted hours.
`done` jobs are not continually revalidated against later raw
revisions; correction detection and approved historical recomputation remain
release work. Do not treat this scheduler as a final historical consistency
solution or enable it on unrelated legacy points.

Admin path: Settings -> Data sources -> Jishiyun workbench -> source ->
**Business hour processing**. The administrator-only paginated
`GET /datasources/{source}/acquisition/business` returns status, gate code,
operator, attempts, counts and retry time, but not lease tokens or credentials.
`status` can filter queued/running/blocked/done/error.
`POST /datasources/{source}/acquisition/business/{job}/control` with
`{"action":"retry_business"}` requeues blocked/error jobs after explicit UI
confirmation. Repeating a queued request is idempotent; running/done jobs are
rejected. Requeue preserves the gate and original approval actor until the
next attempt, audits the requesting administrator, and performs no supplier
request or synchronous business write.

Real local verification: jobs for runs 12 and 13 completed automatically.
Run 12 replay added zero rows. Run 13 wrote 45 registers and nine hourly rows
for Beijing `[16:00,17:00)`, totaling **85.720000 kWh**, independently reconciled
against raw boundaries. Run 15 initially remained blocked awaiting the 18:00
boundary, with no false zero hour. The synthetic source 1 had no jobs. These
results supersede the earlier single-hour/manual-only checkpoint above.

At the next `19:40` acquisition tick, run 16 successfully collected
`[18:00,19:00)` Beijing time and supplied the missing 18:00 boundary. An audited
administrator requeue expedited job 4, and the worker changed run 15's business
job from blocked to done without another provider request from that retry.
The `[17:00,18:00)` hour totaled **71.060000 kWh**. There are now 27 hourly
rows across the three accepted hours; every meter matched independent SQL
boundary differencing. The newest hour still waits for its own end boundary.
Timed five-minute recovery is unit-tested; the real recovery was expedited
through the administrator requeue rather than claimed as a timer-only test.

Regression: 234 worker, 67 API and 40 controller tests passed. Browser rendering
used the actual template/controller with synthetic responses; authenticated
real API and MySQL verification were performed separately.

### Nine-meter pilot settlement

The user approved **test-only** CNY 2.3/kWh and 1.5 kgCO2e/kWh. A separate
`PILOT TEST Jishiyun source 4` energy category, cost center and tariff are
associated with only the nine enabled meters; the nine paused meters and
shared defaults are unchanged. The local tariff covers Beijing 2026-10-16,
ending at 2026-10-17 00:00. Expiry blocks settlement rather than assuming a
zero/default rate. Do not extend it or promote test results to invoices silently.

Trusted operators can preview or explicitly first-write settlement:

```powershell
docker exec myems-jishiyun-local-worker-1 python worker.py business-settle --run-id 17 --actor 1 --test-only
```

Add `--apply` only for an explicitly approved first write. The command requires
an existing energy audit, rechecks five-point raw values/revisions and legacy
register/hour values under the source lock, and requires exactly one full-hour
CNY tariff and one rate covering the entire local hour. Gaps, overlaps and
intra-hour price changes block; quarter-hour time-of-use splitting is not yet
implemented. Billing and carbon are written together with their audit in one
cross-schema InnoDB transaction. Stored values use six decimal places with
half-up rounding, not a final two-decimal invoice policy. Replays verify
parameters, outputs and proof; changed parameters/raw revisions, missing output
and unowned rows require review, never an implicit overwrite.

The explicit **test-only** `run --pilot-test-settlement` option requires business
scheduling and labelled/isolated test parameters. It settles a newly claimed
job after energy normalization; an energy commit and a downstream commit are
separate crash-recoverable transactions. `settlement:<gate>` remains blocked
for retry after five minutes and may already have energy rows. A crash before
job acknowledgement replays both proofs. Previously done jobs are not revisited
when this option is first enabled; use an approved settlement command or a
complete overlapping backfill for those hours. Disabling this option stops
future pilot settlement attempts, not energy collection or existing test data.
Production tariffs are not enabled by this flag. The two legacy physical-meter
billing/carbon loops exclude `JISHIYUN_REGISTER` associations to prevent dual
writers; upgrade those loops before any mixed-service deployment.

Real run 17 collected nine meters over Beijing `[10:00,18:00)`: 288 expected
and valid raw slots, no missing/invalid/unexpected slots. It reused the prior
27 audited meter-hours, added 45, and automatically settled all 72 meter-hours.
There are 297 unique cumulative rows including shared boundaries, and 72 rows
in each energy/billing/carbon hourly table for that window. Independent SQL and
27 authenticated report checks agree: **579.130000 kWh**, **CNY 1331.999000 test
cost**, **868.695000 kgCO2e test emissions**. Two-connection concurrent replay
added zero rows and retained one energy and one settlement audit for run 17.
The three meter reports bypass Redis reads/writes for staged points, preserving
legacy caching without waiting for a 30-minute stale result or flushing Redis.
Three real XLSX exports retained the PILOT TEST label. Exact-key stale Redis
sentinels were ignored and not overwritten; original key state was restored.
Regression: 252 worker + 70 API + 40 controller + 1 aggregation selection test
(covering both billing and carbon queries), **363 tests passed**.

Local repeatable tools, mounted at `/acceptance`:

- `configure_pilot.py`: default preview, explicit actor/time range/`--apply`,
  exact nine-address guard, isolated test configuration and audited associations.
- `pilot_reconcile.py`: read-only five-point/hour/settlement/report reconciliation;
  reads ignored local login credentials without printing them.

This is meter-level pilot acceptance, not 24-hour continuous observation,
supplier cross-day/year acceptance, space/equipment aggregation, approved
historical correction or production readiness.

## Local container acceptance (2026-10-15)

The dedicated [local Compose stack](../others/docker-compose-jishiyun-local.yml)
is separate from all existing deployment files. Project name:
`myems-jishiyun-local`. It currently runs MySQL, Redis, API, Admin, the acquisition
worker and a **synthetic HTTPS provider**. All six containers passed their
healthchecks. Only `127.0.0.1:8000` (API) and `127.0.0.1:8001` (Admin) are published;
MySQL/Redis/mock have no published host ports. Existing port 80 was not changed.
No old normalization, aggregation, cleaning, Modbus or real-provider collector
was started.

Open <http://127.0.0.1:8001/>. The local user is `administrator`; the generated
random password is in `others/jishiyun-local/state/login.json`. This is not the
installation default password. Local database/mock credentials are generated in
`others/jishiyun-local/.env`; both it and `state/` are git-ignored. Do not commit,
share or use these credentials outside this disposable environment. The API
image deliberately copies application modules instead of the developer `.env`.
The lab uses root database access for schema/transaction acceptance, not a
production privilege model.

The original Docker Hub connection timed out and the original PyPI wheel download
also timed out. Local configuration therefore selects public base-image downloads
from `docker.m.daocloud.io/library` and the HTTPS Tsinghua PyPI mirror. No global
Docker/proxy settings were changed; no image or source code was uploaded. Worker
build arguments `PYTHON_IMAGE` and `PIP_INDEX_URL` retain official defaults outside
this local override. TLS certificate validation was not disabled.

Verified against the real MySQL driver and containers:

| Check | Observed result |
|---|---|
| Fresh base schema and extension installation | Nine system extension tables + one historical table, all InnoDB |
| Additive extension script re-run | Completed without removing tables/data |
| Separate-connection advisory lock | Second connection excluded until first releases the lock |
| Cross-schema rollback | Synthetic system event and historical reading both rolled back |
| Archive HTTP -> approval -> queued initialization | 70 meters, points, links and disabled mappings created |
| One-hour 70-meter collection | 280 expected slots |
| Four absent meters | 264 valid slots, 16 active per-slot missing alerts, partial run |
| Missing data supplied on subsequent collection | 280 raw rows, 16 alerts recovered, done run |
| Identical collection re-run | 280 duplicates; still 280 rows and revision 1 |
| Missing response after recovery | 16 alerts reactivated; prior valid raw rows retained |
| Authenticated cancellation | Token cleared, stale worker renewal rejected, duplicate cancellation has one audit event |
| Browser | Task pause/resume, alert acknowledgement, run counts and 50+20 initialization-item pages verified |
| Pending point compatibility | 70 pending-register points return null normalized latest values, HTTP 200 |
| Safety after testing | Zero enabled mappings, zero ENERGY_VALUE points/business energy rows; hourly scheduler remains off |

Run IDs 1–6 retain the archive, partial, recovery, duplicate, reactivated-missing
and cancelled cases. The UI deliberately retains 16 active synthetic missing
alerts, one acknowledged, for manual inspection; acknowledgement is not recovery.
Machine-readable test results are in ignored `state/result.json`.

Container acceptance found and fixed two integration defects: Falcon requires the
same `{id_}` parameter name as the existing data-source routes; and the existing
point-list endpoint must explicitly return null for `JISHIYUN_REGISTER` rather
than raising an unbound-variable error or inheriting another point's latest value.
Both cases now have regression coverage. At that checkpoint, 200 worker,
21 API and 10 Node tests passed; the subsequent initialization UI/API brings the
initialization checkpoint count to 203 worker, 29 API and 14 Node tests.
The task-editor increment below brings the current count to 203 worker, 37 API
and 17 Node tests.

To inspect or restart the **already initialized** local environment, from the
repository root:

```powershell
docker compose --env-file .\others\jishiyun-local\.env -f .\others\docker-compose-jishiyun-local.yml --profile acceptance ps
docker compose --env-file .\others\jishiyun-local\.env -f .\others\docker-compose-jishiyun-local.yml up -d --no-build --wait db redis api admin mock worker
```

To stop it while retaining the database and fixtures:

```powershell
docker compose --env-file .\others\jishiyun-local\.env -f .\others\docker-compose-jishiyun-local.yml --profile acceptance stop
```

Do not add `--volumes` to a teardown or re-run base install scripts against
populated databases. `init-db.sql` sources destructive base installers **only
for a fresh dedicated volume**; normal restarts do not reinitialize it.
`bootstrap.py` refuses existing data-source fixtures. The `checks` service is a
short-lived test writer with a writable ignored `state/` bind and tmpfs; the live
worker remains non-root/read-only. `acceptance.py workflow` requires a pristine
fixture; it is not a repeatable reset command on the populated acceptance data.
The explicit `resume-after-missing` command only accepts the two-run checkpoint.
The mock certificate is local-only, valid for 30 days from bootstrap.

The subsequent HTTP initialization smoke test also passed: authenticated archive
submission/replay, worker fetch, 50+20 safe preview pages, failed batch approval
rolling back all items, repeated approval producing only 70 actor-attributed
events, and repeated initialization submission reusing 70 existing mappings in
run 7 without creating additional business objects or raw rows. This test uses
the same shared transactions as the API and worker. It can be invoked against
the already populated **synthetic** fixture with:

```powershell
docker compose --env-file .\others\jishiyun-local\.env -f .\others\docker-compose-jishiyun-local.yml run --rm --no-deps checks /acceptance/acceptance.py http-initialization
```

Each invocation adds an audited run; it does not reset data. Never run the local
acceptance helpers against a production database.

These checks are functional acceptance of the **implemented slice**, not a
capacity benchmark, production readiness or validation of hour/billing/carbon
results. Real provider authentication has not been performed; HTTPS has a
certificate-name mismatch, and the new explicit HTTP test mode has only synthetic
test coverage. The supplied real test key was not used. Unfinished API/UI
remaining initialization/configuration workflows, ownership/normalization guards and historical downstream
recomputation still require development and later full-version acceptance.

## Administrator monitoring/control workbench

Open **Settings > Data source > Jishiyun acquisition**, choose a data source, then
select tasks, runs, completeness alerts or audit events. Runs link to per-meter
initialization results. All displayed timestamps are UTC. Tables use 50-row
keyset pages and explicit refresh, not a background polling loop. A load failure
is shown as an error, not an apparently empty successful response. Existing
data-source tabs and their default selection are unchanged.

The existing Falcon API serves these resources (no provider HTTP calls or API keys):

| Method | Relative API path | Behavior |
|---|---|---|
| GET | `datasources/{source}/acquisition/tasks` | Task identity, enabled state and scheduling fields |
| GET | `datasources/{source}/acquisition/runs` | Status, window, attempts, expected/valid/missing/invalid/unexpected counts |
| GET | `datasources/{source}/acquisition/alerts` | Address and 15-minute timestamp, quality type, recovery and acknowledgement |
| GET | `datasources/{source}/acquisition/events` | Safe audit identity, type, operator and timestamp |
| GET | `datasources/{source}/acquisition/runs/{run}/items` | Per-meter initialization status and local IDs |
| POST | `datasources/{source}/acquisition/{collection}/{id}/control` | Explicit audited control described below |

GET accepts `limit` (1–200, default 50), optional `before_id` (exclusive descending
ID cursor), and `status` for runs/alerts only. Response shape is
`{items: [...], has_more: boolean, next_before_id: id|null}`. Use the returned
cursor when `has_more` is true. Unknown collections/statuses and invalid IDs or
page sizes fail explicitly. Raw archives, configuration/credential JSON and
unbounded event detail payloads are not returned.

Control body must contain **only** `{"action": "..."}`:

- `tasks`: `enable` / `pause`. Pausing prevents new claims, not an in-flight
  transaction or HTTP request. Enable does not turn on `run --schedule`, submit
  an initialization or activate mappings.
- `runs`: `cancel` for preview/queued/running. Clears the lease token, expiry and
  retry eligibility; the old worker cannot commit further results. Already
  committed samples and per-meter objects remain. Completed runs return 409.
  Cancelling one backfill child is **not** cancellation of the parent batch.
- `alerts`: `acknowledge` for active alerts. Stores the authenticated user and
  UTC time; it never marks a missing/invalid slot as recovered. Recovery remains
  driven by verified data. Recovered alerts return 409.

All data and control routes call the existing `admin_control`; missing/expired
sessions, non-administrators and read-only administrators are rejected by that
existing policy. Actor identity comes from authenticated request context, never
the request body. Each query is source-scoped; cross-source run/item access fails.
Controls take the same source-row-first lock as the worker, require InnoDB, and
commit the state change and audit event together. Identical controls are
idempotent and do not append duplicate events. Audit write failure rolls back
the change. Driver errors produce a fixed safe HTTP 500 and server error log.

This is not the complete workbench. Business activation, explicit
reuse/reconciliation editing and health/matrix views still require subsequent
development. Backfill batch submission/progress/cancellation is described below.

### Administrator backfill batches

Choose an enabled `energy_backfill` task, click **Submit backfill**, enter an
explicit-timezone ISO `[start,end)` window on 15-minute UTC slots and review the
snapshot before confirming. A batch spans at most 31 days, each child at most one
day, and the whole range must be older than the task's configured delay.
Confirm provider retention before submitting; "about one year" is not treated as
a precise guaranteed expiry date. Scope uses the existing queue rules: ready
mappings with effective ranges that overlap each child, including disabled but
explicitly end-dated historical mappings. Callers cannot post arbitrary addresses,
scope JSON, credentials or operator IDs. New mappings still need the separate,
unfinished readiness/activation workflow.

| Method | Suffix after `datasources/{source}/acquisition/` | Request/result |
|---|---|---|
| POST | `backfill` | Exactly `{task_id,request_key,window_start_utc,window_end_utc}`; UUID, authenticated actor and normalized range identify the resumable batch; HTTP 202 means enqueued, not collected |
| GET | `batches` | Standard bounded descending keyset list, default 50, maximum 200 |
| GET | `batches/{id}` | Live `expected_children`, `submitted_children`, `unsubmitted_children`, `child_status_counts`, `run_ids`, `complete`, cancellation flag, window and submitting user |
| POST | `batches/{id}/resume` | Empty object; original submitting administrator only; derives identity/window from the stored parent |
| POST | `batches/{id}/cancel` | Empty object; authenticated administrator; fences open children and prevents remaining submissions |

The API reuses `enqueue_backfill` and `batch_status`, not a second implementation
of dispatch/cancellation. Provider calls still occur only in the worker, under
the shared request quota. Child transactions commit independently: an interrupted
submission may leave a durable parent or prefix. An error is not a rollback
promise. Retry the exact same request or use **Resume submission** in the batch
list after a reload; do not issue a new UUID for the same uncertain request.
The form freezes sent identities/windows and retains drafts across source/view
switches within the current page. HTTP 400 is reserved for input validation before
parent creation; it unlocks corrections. Scope conflicts after queue entry return
409. Successful cancellation or full submission releases the matching draft.

Resume only fills missing child submissions; it does not retry finished
partial/error/cancelled runs. Use a separately reviewed new backfill request for
another collection attempt. `complete` is true only if all expected children are
submitted and `done`; it never means business normalization passed.
Cancellation preserves committed readings and is idempotently audited.
Pausing a task does not cancel its parent batch. Task execution settings and
mapping ranges are frozen even when a parent exists with zero children.

Validation: 203 worker/library, 55 API and 27 front-end tests passed (285 total).
Eight new API tests cover the HTTP boundary/delegation, six new UI tests cover
confirmation/recovery, and one mapping test covers parent-only history freezing.
Real MySQL HTTP checks resume a committed one-of-two-child prefix, replay without
duplicates, cancel a claimed child, reject the old lease and retain one actor
cancellation audit. Only the disposable synthetic source is removed afterward.
Browser checks cover the form, confirmation, visible empty-scope failure,
locked retry draft, parent progress and cancellation. Cancelled synthetic batch
2 remains for inspection; it has no child runs. Successful form-to-worker
collection of a fresh batch is not yet browser-verified.

```powershell
docker compose --env-file .\others\jishiyun-local\.env -f .\others\docker-compose-jishiyun-local.yml run --rm --no-deps checks /acceptance/acceptance.py http-backfill
```

### Administrator task configuration

The task list now provides **Create task** and **Edit task**. Supported types are
`meter_initialize`, `energy_collect` and `energy_backfill`; undispatched
`archive_sync`/`recalculate` types cannot be created here. New tasks are always
paused, and enabling remains a separate audited operation. Editing cannot
silently change enablement, data source, task type, device scope or credentials.

| Method | Source-scoped suffix after `acquisition/` | Request/result |
|---|---|---|
| POST | `tasks` | `{request_key,configuration}`; UUID replay with the same configuration and authenticated actor returns the original task ID |
| GET | `tasks/{id}` | Allowlisted `configuration`, `is_enabled` and `has_runs`; arbitrary parameters/secrets are never returned |
| POST | `tasks/{id}` | `{configuration,expected_configuration}`; optimistic comparison prevents stale overwrites |

Configuration contains exactly `name`, `task_type`, `schedule_minute` (0–59),
`delay_minutes` (0–65535), `lookback_hours` (1–24),
`provider_utc_offset_minutes` (−840–840, divisible by 15) and `encoding`
(`json`/`form`). Numeric values must be integers, not booleans or numeric strings.
The scheduler's existing validation is reused. UTC offset is explicitly reviewed;
the UI prepopulates +480 and the existing 40-minute/3-hour defaults for confirmation.
Only `energy_collect` is automatically scheduled, and only with worker
`run --schedule`. Creating an initialization/backfill task does not submit a run.
Device scope and collection start remain separate mapping configuration.

All task mutations require administrator authorization and source-first InnoDB
locks. Task data and before/after audit records commit together; audit failure
rolls back the change. Identical edit replays do not append events. A task must be
paused and have no preview/queued/running runs before a real edit. After any run
exists, execution settings are frozen and only the name may change; create a new
task for a different execution policy. This deliberately avoids changing already
captured run settings or reusing the same scheduled-tick UUID for a new window.
It also prevents edits racing an in-flight initialization approval.
Unsupported extra stored parameters cause a conflict rather than being erased.
Responses use 400 for invalid requests, 404 for missing/cross-source tasks,
409 for stale/state conflicts and safe logged 500 for database failures.

Local validation: real MySQL HTTP create/replay/edit/replay and stale/type-conflict
checks passed for paused task 4, with exactly two actor-attributed events.
The browser also created task 5, confirmed the displayed snapshot, changed its
name/lookback and saved; both synthetic probes remain paused. No collection run
was submitted, mappings remain disabled and existing 280 raw readings were
unchanged. Repeatable HTTP checks (each invocation adds one paused synthetic task):

```powershell
docker compose --env-file .\others\jishiyun-local\.env -f .\others\docker-compose-jishiyun-local.yml run --rm --no-deps checks /acceptance/acceptance.py http-tasks
```

### Administrator mapping range configuration (staged only)

The workbench now lists source-scoped mappings with 50-row keyset pagination and
provides **Verify ownership / configure range**. `GET acquisition/mappings/{id}`
returns allowlisted configuration, local meter/point names, `ownership_verified`
and `activation_available:false`; raw archive JSON is never exposed.
`GET acquisition/mappings` uses the same bounded collection contract as other lists.

`POST acquisition/mappings/{id}` accepts exactly
`{collection_start_utc,collection_end_utc,expected_configuration}`. The expected
configuration is the complete snapshot returned by the detail endpoint.
Start is required; end may be null. Both must be timezone-aware ISO timestamps,
aligned to 15-minute UTC slots, with start inclusive and end exclusive.
The API stores normalized UTC timestamps and rejects naive timestamps, seconds,
subseconds, inverted/empty ranges, unsupported fields and stale snapshots.

Every detail read and edit verifies the mapping archive ID/address, point source,
register address/field, `JISHIYUN_REGISTER` type, identity ratio/offset,
non-virtual trending configuration and exactly one exclusive meter-point link.
Missing, shared, duplicate or changed ownership returns 409 rather than repairing
or rebinding local objects. Mapping edits require disabled, non-conflicted state.
Source-wide open runs or any energy-collection/backfill run history freeze range
changes until the audited correction/recomputation workflow exists. Orphaned run
history also blocks changes. An identical range replay is a no-op without a new
audit event; ownership and disabled/non-conflicted checks still run.

Source-first InnoDB locks cover all mapping/local entity tables; range update and
actor-attributed `admin_mapping_range_updated` audit commit together. Audit failure
rolls back the range. This editor never changes `is_enabled`, configuration status,
point type, local identities, credentials or business tables. It does not activate
raw collection or business normalization. It changes no cached meter/point fields;
initialization cache invalidation remains a separate unfinished increment.

### Raw mapping collection control (2026-10-16)

`GET acquisition/runs/{id}/items` returns `detail_kind:archive_items` for
initialization, or `energy_current_window` for an energy window. Energy items
contain current source-window raw registers (`forward_active_register` as exact
decimal text), UTC times, quality, revision and first/last writer IDs. They do
not expose response JSON or credentials. Source/run ownership, a 200-row page
limit and descending-ID pagination apply. The view is not immutable run-result
storage: later backfill can update rows, and other same-source runs may cover
different meters in that window. Use run counters for original completeness;
missing samples are not synthesized and last writer does not prove response
membership. Hour-end cumulative boundaries are still required before hourly
business values can be computed.

The Admin single/batch mapping time pickers use fixed UTC+8 and submit explicit
`+08:00` timestamps; stored/listed times remain UTC. No browser-local timezone
assumption is used. Backfill window forms are unchanged.

`POST acquisition/mapping-batch` accepts exactly
`{action,items,collection_start_utc,collection_end_utc,confirm_kwh_direct}`.
Each of 1–200 unique items is `{id,expected_configuration}`, with the complete
mapping snapshot (the nine mapping configuration columns, not extra list fields).
`edit_range` uses a shared aware start/optional end and requires
`confirm_kwh_direct:false`; `enable_raw` requires true and both times null,
because it uses saved per-meter ranges. Unsupported actions, duplicates and
oversized lists are rejected. Source lock, sorted mapping locks and a single
transaction ensure writes/audits are all-or-none. An item failure returns 409
with the mapping ID and safe reason, never a partial-success response.
Single and batch endpoints share the same guards and transaction helpers.
UI selections survive paging, clear on source change or success, and require
reselection after a range write before enabling. Batch pause is not provided.
The repeatable local `http-mapping-batch` acceptance command validates range
conversion, replay, atomic rollback and actor audit in disposable synthetic
fixtures without touching existing mappings or calling a provider.

The supplier unit is confirmed as kWh: use `ZYGDNSZ` directly, with ratio 1 and
offset 0. No PT/CT multiplication or automatic unit conversion is performed.

`POST acquisition/mappings/{id}/raw-collection` accepts exactly:

```json
{
  "action": "enable_raw",
  "confirm_kwh_direct": true,
  "expected_configuration": {}
}
```

Replace the empty object with the complete configuration from the mapping detail
endpoint. Supported actions are `enable_raw` and `pause_raw`. Both require an
authenticated administrator, explicit direct-kWh confirmation, exact `kWh` point
unit, verified staged-register ownership and pending/ready non-conflicted state.
Enabling also requires a saved, aligned collection start and valid end. The UI
provides these controls in the ownership/range editor with a second confirmation.

Enabling marks the mapping ready/enabled without changing `JISHIYUN_REGISTER`,
creating a run, calling the provider or activating normalization. Pausing keeps
readiness and the original range. End-dated disabled mappings remain available
for historical backfill; pause tasks and cancel open runs/batches to stop all work.
Existing enabled tasks/schedulers may collect after raw enablement, so pause them
before changing scope during acceptance.

Source-first transactions serialize state changes with queue submissions.
Open source runs and any uncancelled backfill parent block changes, including
fully submitted/completed parents conservatively: cancel retained parents first
to prevent later resume under changed scope. Cancellation retains committed data.
Stale snapshots return 409, identical replays do not duplicate audit, and audit
failure rolls back state. `business_activation:false` is always returned.
The detail field `activation_available:false` still denotes unavailable business
activation, not absence of the separate raw control. Range/history freeze rules
are unchanged. Updated API/Admin are now deployed in the local acceptance stack;
batch behavior also passed disposable-source MySQL/HTTP acceptance. Browser
visual checks and live-provider collection are not claimed, and no real mapping
is automatically enabled.

Validation includes nine API behavior tests and four front-end flow tests.
The local `http-mappings` acceptance command verifies real MySQL/HTTP pagination,
ownership, range edit/replay, actor audit and conflict protection in a disposable
synthetic-only source, removing only that fixture afterwards. The retained 70-meter
source stays disabled with 280 raw readings. Browser verification covers the
mapping list/detail and historical-range refusal, not a fresh-source successful
save through the form.

### Archive initialization in the administrator UI

For an existing enabled `meter_initialize` task, click **Fetch archive** and
confirm. The API enqueues the request; only the independent worker calls the
provider. Refresh Runs until the status is `preview`, then open **Archive
preview**. Select the current page or individual rows, navigate to select more,
and review the selected meter names. At most 200 meters are submitted at a time.
Select existing energy-category/cost-center records, explicitly select the
statistics flag and fill hourly limits, register units and register upper limit.
No category, cost center, unit or limit is guessed. Limits remain decimal strings
through the browser/API boundary.

Approve and confirm the fixed configuration snapshot, then queue and confirm
the initialization. Changing names/settings or selection invalidates the UI's
approval. The worker rechecks all selected approvals and references under its
lease before creating/reusing entities. Existing mapped entities are validated,
not overwritten or rebound by provider name. Unmapped name conflicts appear in
per-meter execution results and require reconciliation. All newly created
mappings remain disabled and points remain `JISHIYUN_REGISTER`.

| Method | Source-scoped suffix after `acquisition/` | Behavior |
|---|---|---|
| POST | `initialization` | `{task_id, request_key}` queues archive fetch; stable UUID replay returns the same run ID |
| GET | `initialization/{run}` | Safe archive identity/name/area, approval and existing local IDs; `limit` 1–200, descending `before_id` |
| POST | `initialization/{run}/approve` | `{items:[{address,configuration}]}`; 1–200 distinct items, one atomic approval transaction |
| POST | `initialization/{run}/submit` | `{addresses:[...]}` freezes 1–200 approved addresses for worker initialization |

All four routes require the existing administrator policy. The authenticated
actor is never supplied by clients. Invalid state/source conflicts return 409;
invalid values return 400; driver failures log a safe error and return 500.
Preview responses do not return raw provider archives or credentials. Approval,
submission and fetch events include the authenticated actor in the audit column.
No provider key/base URL is needed in the API process.

### Shared Python package and builds

The shared code lives in `myems_jishiyun/`, installed from `pyproject.toml`.
API and worker images install the same package; no `sys.path` modification or
duplicate queue/initialization implementation is used. For local development,
install once in the service/test interpreter before testing:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .\myems-jishiyun
```

The top-level `worker.py`, `verify.py` and `health.py` retain existing command
entry points. API Docker builds now use the repository-root context (see API
README); worker/verifier builds retain this directory as their context.

Validation (synthetic data only):

```powershell
Set-Location myems-api
..\.venv\Scripts\python.exe -m unittest discover -p "test_jishiyun*.py" -v
Set-Location ..
node --test myems-admin\test-acquisition.js
```

55 Falcon HTTP/transaction tests cover real administrator rejection paths,
source isolation, bounded pagination, precise gap counts, safe response fields,
idempotent controls, fencing and audit rollback. Column names are derived from
the actual extension DDL in a SQLite behavior adapter. Twenty-seven dependency-free Node
tests cover controller state, stale responses, confirmation, error reporting,
pagination, authentication headers and English/Simplified/Traditional Chinese
translations (other locales use English for this feature). The 203 worker
regression tests remain separate. These unit tests alone do not prove real MySQL
locks or browser rendering; the separate local acceptance above exercises those
implemented paths, while full-version acceptance remains pending.

## Confirmed contract

- Only `/NH2/AmmeterBasicInfo` and `/NH2/ElectricQuantity` are in scope.
- Request `valueType=SJZ`, `dateType=mi15`, `meterAddress=""`.
- Consume **ZYGDNSZ**, the forward active cumulative register, not ZYGDN.
- Actual response envelope: `IsSuccess`, `Code`, `Msg`, `Data`, `Count`.
- API keys must come from deployment secrets/environment, never source files,
  test fixtures, database connection JSON or logs.
- Anonymous HTTP POST probes reached both supplied endpoints (HTTP 200), but
  that is not proof of authenticated API success or compatible response data.
  HTTPS probing failed certificate-name validation on 2026-10-14. No supplied key
  was transmitted. Valid encrypted transport remains recommended; the later
  explicit HTTP test exception is documented below with its plaintext risks.
- The client uses the documented `API_KEY` header (not `APIKEY`).
- Provider timezone must be passed explicitly; its actual offset, timestamp
  meaning, unit and inclusion of CT/PT ratios remain contract checks.

`myems_jishiyun/contracts.py` provides request parameters, envelope/archive/reading validation,
Decimal conversion, UTC conversion and `[start, end)` coverage checking.
Invalid numeric readings retain their device/timestamp identity for alerts.
Invalid envelopes or unidentifiable rows raise `ContractError`; the worker
records a fixed failure code rather than treating them as empty successful data.

Coverage uses the configured device scope, not devices returned by the API.
Duplicate records never fill missing slots; conflicting duplicates invalidate
their slot. A complete four-slot report **does not establish hourly consumption**:
hourly differencing still needs validated boundary registers. Returned zero
registers and unchanged registers are valid values, unlike absent records.

## Targeted tests

Python 3.10+; these tests use only the standard library and synthetic data.
From this directory, using the project's selected interpreter:

```text
python -m unittest discover -s . -p "test_*.py" -v
```

No production device names, addresses or credentials are embedded in the tests.
All 203 worker/library tests passed on the selected Python 3.10.11 interpreter. The suite covers
contracts, client, verifier, scope planning, persistence and queued execution,
including 70 meters'
280 hourly records / 6,720 daily slots and the 33/29-meter missing-data case.
These are record-count checks, not measured MySQL throughput guarantees.
Persistence behavior tests use a small SQLite adapter with synthetic schemas:
it adapts bind syntax, datetime/Decimal values and strips `FOR UPDATE`. This
**does not validate MySQL syntax, actual DDL, locks, isolation or concurrent
writers**. Its simulated engine metadata is not evidence about any real server.
Queue tests additionally simulate `GET_LOCK`/`RELEASE_LOCK` and use one shared
SQLite test connection; they verify control flow, not real independent-session
exclusion or crash release behavior. Tests include guarded HTTP retries,
expired/cancelled run rejection, failure-audit outages, same-credential grouping,
submission rollback and the queue-to-mocked-HTTP-to-raw/alert commit path.
Scheduler tests cover due-minute boundaries, provider offsets, delayed data,
restart idempotency, stopped task submission and failed/resumed backfill prefixes.
Heartbeat tests use an injected clock and dedicated temporary files; they do
not prove a real container's Docker health state.
An actual MySQL acceptance run remains required. A previous live TLS check using
a synthetic key failed certificate validation and exited with code 1 as intended;
the user-supplied key was not used.

## Read-only verification client

`myems_jishiyun/client.py` implements POST requests for the two approved endpoints. Encoding is
explicitly `json` (default) or `form`; it does not guess by retrying another
encoding. Live encoding support still requires authenticated verification.
Requests default to HTTPS with certificate validation and refuse redirects.
The explicit HTTP test exception below does not disable TLS checks or
automatically downgrade a failing HTTPS request.

### Explicit supplier HTTP test transport

At the user's request, both existing methods support the supplier's HTTP test
environment, but only with an explicit opt-in:

```dotenv
JISHIYUN_BASE_URL=http://nhapi.yunjichaobiao.com/WebApi
JISHIYUN_ALLOW_HTTP_TEST=true
JISHIYUN_API_KEY=
```

Inject the real test key through deployment secrets or an untracked local
environment file; the blank above is intentional. Never commit a real key.
The worker Compose file reads `myems-jishiyun/.env`; local Python entry points
read process environment, not that file automatically.
The verifier still takes the base URL explicitly:

```powershell
# After securely injecting JISHIYUN_API_KEY into the process:
$env:JISHIYUN_ALLOW_HTTP_TEST = 'true'
.\.venv\Scripts\python.exe .\myems-jishiyun\verify.py --base-url 'http://nhapi.yunjichaobiao.com/WebApi' --utc-offset='+08:00'
```

The command above fetches archives only. Add the verifier's explicit start/end
and expected-address options to check electricity and completeness. Confirm the
provider timezone before a real call; `+08:00` is an example, not a new contract
confirmation.

`JishiyunClient.archives()` posts to `/NH2/AmmeterBasicInfo`;
`JishiyunClient.readings(start,end)` posts to `/NH2/ElectricQuantity`.
Both send `API_KEY` as a request header, never a URL/body parameter.
The direct Python client accepts keyword-only `allow_http_test=True`; the
worker and verifier derive that permission from `JISHIYUN_ALLOW_HTTP_TEST`.
Only literal `true`/`false` are accepted. The default is `false`.
HTTP opt-in is restricted to `nhapi.yunjichaobiao.com` on port 80 and the
existing `/WebApi` base path; other hosts/ports, embedded credentials, query
strings and redirects remain rejected. JSON/form encoding, bounded timeouts,
response validation, rate limiting and idempotent raw persistence are unchanged.

**HTTP sends the API key and returned meter data in plaintext.** Test mode logs
a fixed warning without secrets. It is not a secure tunnel and is not recommended
for production. HTTPS certificate errors never trigger HTTP fallback.
Disable the test permission when a valid HTTPS endpoint becomes available.
This increment was verified using six synthetic/mock tests plus the existing
203 worker tests (209 passing), without sending the supplied key or contacting
the real provider. It neither enables mappings nor activates business processing,
and running containers were not switched to this test transport.
Responses are limited to 16 MiB and decoded as UTF-8 (a UTF-8 BOM is accepted).

Calls are serialized with at least seven seconds between starts, including
retries. The default is three attempts with a 30-second per-attempt timeout.
Only network/HTTP framing errors and HTTP 429/502/503/504 are retried.
Numeric and HTTP-date `Retry-After` delays are honored; delays over five minutes
or malformed delays fail explicitly so a future scheduler can defer the run.
Business/contract errors, authentication errors and certificate validation
failures are not retried. Raw response bodies, keys and provider messages are
not logged.

**This limiter is process-local.** Run only one verification process per provider
credential, and do not overlap this standalone verifier with another collector.
The queued worker uses the shared guard described below; the standalone verifier
does not join that database quota.

`verify.py` reads `JISHIYUN_API_KEY` from the process environment. Inject it using
local deployment secrets; it is not a command-line argument. For a local
interpreter invocation, `.env` is not automatically loaded. Example from this
directory, after configuring the environment:

```powershell
python .\verify.py --base-url "https://provider.example/WebApi" --utc-offset="+08:00" --encoding json --start "2026-10-01 12:00:00" --end "2026-10-01 13:00:00" --expected-addresses "synthetic-meter"
```

Use a real, certificate-valid provider URL and the confirmed provider timezone;
the example hostname, date and address are placeholders, not live configuration.
Omit the time window to verify only archives. Verification windows are capped at
one day; historical initialization is not implemented by this command.

Output is a JSON summary of counts, not device details or raw samples. Explicit
`--expected-addresses` enables completeness checking against configured scope;
without it, completeness is **not evaluated**, never inferred from the response.
The hour's end-boundary register is not verified by the four-slot coverage check.
Exit codes: `0` for valid responses with no detected issue, `1` for configuration,
transport or contract failure, `2` for detected missing/invalid/unexpected slots
or invalid readings. Exit `0` without scope is not a completeness certificate.

## Isolated verification container

The Dockerfile uses only standard-library Python and runs as a non-root user.
Its allowlist build context excludes `.env`, tests, logs and local credentials.
The separate [Compose definition](../others/docker-compose-jishiyun-verify.yml)
does not alter existing MyEMS services. Copy `example.env` to the ignored `.env`
file locally and populate it through secret management before deployment.

The default container command displays help and exits. Supply the verification
arguments above when running it. This is a **one-shot verifier**, not a worker:
no restart loop, scheduler, database writes or fake service health indicator.
Compose configuration was validated locally, but image build/start was not:
the Docker Linux engine was unavailable. A build/run acceptance test is still
required; the container must not be reported as deployed.

## Queued-energy worker

[`execution.py`](./myems_jishiyun/execution.py) provides `RunQueue.enqueue`, `claim`, `renew`,
`fail`, a credential-wide `SharedRequestGate`, and `execute_one`.
[`worker.py`](./worker.py) is the executable entrypoint. It polls already queued
energy jobs every five seconds, or processes at most one job with `run --once`.
Add `--schedule` to also submit latest-due hourly windows before each claim.
Scheduling is **off by default**, including in the supplied Compose service.
The worker also dispatches `meter_initialize` runs in explicit `archive_fetch`
and `archive_initialize` phases. Independent periodic `archive_sync` tasks and
downstream recalculation are not implemented in this dispatcher.

## Approved archive initialization

[`initialization.py`](./myems_jishiyun/initialization.py) provides `ArchiveInitializer` with
`create_preview`, `approve`, single-meter `initialize`, `submit_approved`,
`execute_approved` and paged `progress`. The trusted CLI can import an
**explicitly supplied local archive response**, or queue provider archive
fetching for the independent worker. An enabled `meter_initialize` task must
already exist for the configured source.

```powershell
python .\worker.py archive-preview --task-id 3 --request-key "00000000-0000-4000-8000-000000000003" --actor 1 --response-file "D:\local-secrets\archive-response.json" --utc-offset="+08:00"
python .\worker.py archive-approve --run-id 10 --address "synthetic-meter" --actor 1 --configuration-file "D:\local-secrets\approved-meter.json"
python .\worker.py archive-initialize --run-id 10 --address "synthetic-meter" --actor 1
```

Paths, addresses and IDs above are placeholders. Files are read as UTF-8/BOM,
bounded to 16 MiB, never copied into the image; the response must contain the
validated provider envelope. Protect local files as device data and do not commit
them. These commands require DB environment configuration but not an API key.
They are trusted operator tools, not administrator authentication or the final UI.

### Queued archive workflow

Instead of importing a saved response, queue a new provider fetch:

```powershell
python .\worker.py archive-fetch --task-id 3 --request-key "00000000-0000-4000-8000-000000000004" --actor 1
python .\worker.py run --once
python .\worker.py archive-status --run-id 11 --limit 100
```

IDs/addresses/UUIDs are placeholders. `archive-fetch` only writes the queue and
requires no API key; the worker uses the same environment key, transport policy
(HTTPS by default, explicit restricted HTTP test opt-in), source serialization,
shared quota, retry/backoff and lease fencing as
energy collection. Archive fetches have **no energy time window**. Their request
settings are captured from the initialization task. The documented
`AmmeterBasicInfo` endpoint is used, never `ElectricQuantity` for an archive phase.
A reused fetch request UUID returns the original run, including after the run
has moved on to approved initialization; another actor/task/source or a UUID
originally used for another operation is rejected.

Validated archive rows and the transition `running -> preview` commit atomically,
with the fetch lease cleared. Fetching **does not** create meters or enable
collection. An empty successful archive response is an empty preview, not a
completed initialization; an invalid response is an error, never an empty preview.
Network failures follow bounded durable retry. The worker will not claim a
`preview` until its selected items have been explicitly approved and submitted.

After approving the desired items with `archive-approve`, submit a fixed
selection to the worker. Repeat `--address` to select multiple approved items:

```powershell
python .\worker.py archive-submit --run-id 11 --actor 1 --address "synthetic-meter" --address "synthetic-meter-2"
python .\worker.py run --once
python .\worker.py archive-status --run-id 11 --after-id 0 --limit 100
```

Submission validates all selected approvals/references, freezes the address list
and approving actor in the run parameters and transitions `preview -> queued`.
No initialization runs inside the submission command. Replaying an identical
selection/actor is idempotent, even after completion; it does not retry a failed
run. A changed submission is rejected. Once queued, approvals can no longer be
edited and the unleased single-meter command cannot bypass the worker.
The archive-fetch attempt counter is reset when entering approved initialization;
its three-claim recovery limit applies independently to this new phase.
Provider `request_count` remains the actual guarded request-attempt count;
initializing local objects does not make another provider request.

The worker renews its initialization lease between devices, executes one
transaction per device and retains already-committed device outcomes across a
crash. A reclaimed token can replay completed items without duplicating objects;
an old/cancelled/expired token cannot create objects, save false item failures or
finalize the run. The lease is checked again after each device's writes and at
the final conditional run update.

Business/configuration conflicts become fixed-message `conflict`/`error` item
states with audit events and do not prevent other selected devices from running.
Unexpected/driver failures roll back that device, attempt to save a fixed-message
item failure, then terminate the run rather than presenting a successful batch.
If even the failure/audit write is unavailable or the token was lost, the
exception propagates; no successful empty fallback is emitted.
Crash recovery counts prior `created`/`reused`/`conflict`/`error` outcomes once.

The final run becomes `done` only when **all selected** items succeed, otherwise
`partial` for persisted item-level conflicts/errors. The event summary retains
separate created/reused/conflict/error counts. Unselected archive rows remain
`preview`; `done` does not mean every returned archive was selected or that
collection/business calculations are active. Inspect `archive-status` for
selected/successful counts, **all** preview-item status counts and paged item
IDs, identities, local IDs and safe errors. Page size is `1..200`, with `after-id`
and a `has_more` flag; raw supplier payloads/credentials are not printed.
For a terminal/partial initialization that needs corrected approvals, use a new
preview rather than mutating/requeuing the completed selection. UI remediation,
per-item retry controls and an authenticated cancellation endpoint are not yet
implemented.

An approved configuration file must contain exactly these explicit business
fields (synthetic example):

```json
{
  "name": "Approved local meter",
  "energy_category_id": 1,
  "cost_center_id": 1,
  "is_counted": true,
  "hourly_low_limit": "0",
  "hourly_high_limit": "100",
  "units": "kWh",
  "point_high_limit": "999999999"
}
```

Use actual approved category/cost-center IDs, limits and unit, not the example
defaults. Reference existence is checked at approval **and** execution; units,
PT/CT handling and meaningful category selection remain operator/provider
confirmation, not guesses from external names.

- Preview registration/items/audit commit atomically. UUID replay must match
  source/task/operator and the complete immutable archive set, even for an empty
  preview. A changed provider snapshot needs a new preview, not an overwrite of
  user approvals. Duplicate addresses/IDs fail before insertion.
- Preview does not create business objects. Approval requires explicit validated
  configuration and records the actor and approved values. It does not initialize
  the item or enable collection.
- Each initialization transaction creates one meter, one point, their association
  and one uniquely owned external mapping, or rolls all of them back on any error.
  Different devices commit independently. Relevant tables must be InnoDB.
- Identity-based reuse preserves existing local names, limits, point settings,
  mapping state and collection configuration. It checks existing local
  associations and point ownership JSON. A name match is never auto-binding.
  Conflicting external IDs/addresses, broken links, removed mappings or modified
  point ownership require explicit reconciliation; no delete/recreate fallback.
- New mappings are `pending`, disabled and have no collection range. New points
  use **`JISHIYUN_REGISTER`**, not `ENERGY_VALUE`, with ratio `1` and offset `0`.
  This is a deliberate staging type: the existing normalization selector must
  not consume them before the future ownership/completeness safeguards exist.
  PT/CT is not automatically multiplied again. Do not manually flip the point
  to `ENERGY_VALUE` or mark the mapping ready as a shortcut.
- Successful item replay returns the existing outcome and does not create another
  object. Approval is not accepted on a completed item. Initialization does not
  mutate a completed preview's scope or overwrite local settings from new
  supplier archive metadata.

An archive fetch alone stays `preview`; an explicitly submitted initialization
can finish as `done`/`partial` as described above. This stage still does **not**
configure an effective collection start, activate a point, refresh API Redis
caches or start historical energy import. The administrator preview/approval
API and batch UI described above are implemented. Activation/remediation and
cache invalidation remain required before this is the complete end-user workflow.
Real MySQL/container acceptance of acquisition and the new HTTP initialization
path passed on the isolated synthetic fixture; full business acceptance is pending.

## Energy task submission

The explicit submission path requires an existing enabled `energy_collect` or
`energy_backfill` task and ready mappings whose collection ranges intersect the
requested window. It includes stopped meters' historical ranges when an explicit
collection end is retained. Mappings without a collection start, pending mappings
and disabled mappings without an effective historical end are not selected.
Scope and `delay_minutes`/provider offset/request encoding are snapshotted at
enqueue time, never reconstructed from provider responses. Task JSON must specify
`{"encoding":"json"}` or `{"encoding":"form"}`. Other task JSON fields are not
copied into runs, so connection secrets cannot accidentally be copied by this path.

Use a new UUID for a new request, but reuse the **same UUID** when retrying
submission after a network interruption. An identical task/window/operator
submission returns the existing run without re-enqueueing it. A reused UUID with
different arguments is rejected. Retrying an executed `error`/`partial` run is a
**new run with a new UUID**, not a mutation of a completed snapshot.

Configure the empty settings in [`example.env`](./example.env) through local
secret management. A local Python invocation reads process environment, **not**
`.env`; Compose loads the ignored `.env`. Install the pinned
[`requirements.txt`](./requirements.txt) in the deployment environment first.
Only the worker needs the MySQL driver. Both schemas and all workers for a
credential must use the same MySQL server, and all participating tables must be
InnoDB. Supply a least-privilege DB account with required SELECT/INSERT/UPDATE
grants; this code never applies DDL or discovers another installation's secrets.

Example commands from this directory, after configuring the environment:

```powershell
python .\worker.py enqueue --task-id 1 --request-key "00000000-0000-4000-8000-000000000001" --requested-by 1 --start "2026-10-01T12:00:00+08:00" --end "2026-10-01T13:00:00+08:00"
python .\worker.py run --once
python .\worker.py run
python .\worker.py run --schedule
```

The UUID/operator/task above are placeholders. The CLI is a **trusted operator
tool**, not a permission-checked public API: `--requested-by` is an audit
attribution, not authentication. Restrict shell/container/DB access to authorized
operators. The future Admin API must independently enforce administrator access.

### Hourly scheduling and historical splitting

[`scheduling.py`](./myems_jishiyun/scheduling.py) derives the latest eligible schedule tick from
the database UTC clock using each enabled `energy_collect` task's configured
provider offset and `schedule_minute`. It subtracts `delay_minutes`, rounds down
to a **provider-local hourly boundary**, and requests `lookback_hours` ending
there. With minute `40`, delay `40`, lookback `3`, provider time `13:40` submits
`[10:00,13:00)`. At `13:39:59` the latest tick is still `12:40`.
Offsets must be 15-minute aligned within +/-14 hours; fixed offsets, not DST-aware
timezones, are supported. Scheduled lookback is bounded to `1..24` hours.

Submissions use a stable UUID derived from source, task and tick, with
`requested_by_user_id=NULL` for the scheduler. Repeated polling or restart returns
the same run and keeps its original scope. Changes to configuration within an
already submitted tick do **not** silently rewrite that run: if they change its
window, submission fails explicitly; apply such changes at the next tick or
submit a separate manual run. Paused and non-collection tasks are not scheduled.
An invalid schedule or empty effective scope is an error, not a guessed
successful submission. A tick may submit several independent tasks; if a later
one fails, earlier committed submissions remain and replay idempotently.

After downtime only the **latest tick and its configured lookback** are submitted.
There is no durable scheduling watermark and no automatic unbounded catch-up.
Older gaps must be submitted through historical backfill. A previously failed or
partial tick is not automatically retried under the same UUID. Scheduler errors
exit the worker nonzero rather than reporting fresh successful-loop health.

The explicit `backfill` command requires an enabled `energy_backfill` task. It
accepts a timezone-aware, 15-minute-aligned window of **at most 31 days** and
creates children no longer than 24 hours, preserving a partial final day:

```powershell
python .\worker.py backfill --task-id 2 --request-key "00000000-0000-4000-8000-000000000002" --requested-by 1 --start "2026-10-01T00:00:00+08:00" --end "2026-10-04T06:00:00+08:00"
```

The task, parent UUID, user and dates are placeholders. The entire requested
window must be past the configured delay before any child is submitted.
Provider retention is only specified as "about a year"; this command does not
guess a precise cutoff or switch to daily data for older 15-minute requests.
Children retain their original mapping snapshots. Scope must intersect each
child; scope-free gaps fail explicitly and are not silently skipped.

Each child's stable UUID is derived from parent UUID and child index. Re-run
with the **same parent UUID, task, actor and exact window** to resume after
interruption; conflicting existing child arguments are rejected. Use a new
parent UUID for a changed plan. Child submissions commit individually, not as
one all-or-nothing batch. Each committed/existing child ID is printed immediately.
If a later child fails, the command exits `1` after that visible committed prefix.
The tenth extension table `tbl_jishiyun_backfill_batches` persists the original
parent UUID, source, task, operator, exact overall window and child count before
the first child is created. Reusing the parent with a changed tail, actor or task
is rejected even when earlier children happen to match. Parent registration and
cancellation are audited. Child statuses are queried live, not cached as a
success flag on the parent. An interrupted submission can therefore be inspected:

```powershell
python .\worker.py batch --request-key "00000000-0000-4000-8000-000000000002"
python .\worker.py batch --request-key "00000000-0000-4000-8000-000000000002" --cancel-by 1
```

Progress reports expected/submitted/unsubmitted child counts, statuses and run
IDs. Only all submitted children being `done` makes a batch complete; partial
data is not completion. Cancellation is idempotent and source-scoped, fences
queued/running children by clearing their tokens, and prevents any further
children from a concurrently resumed submission. Completed records and audit
history are retained. An in-flight HTTP call cannot be recalled, but its cancelled
lease cannot commit results. The trusted CLI exists; an authenticated Admin
batch API/UI remains to be implemented.

Queue/execution rules:

- Source-row serialization allows at most one live queued-worker execution per
  source; additional containers cannot claim that source's work concurrently.
  Claiming uses database time and a fresh UUID lease, lasting three minutes.
  Before a provider call and before result persistence, the worker renews the
  lease. It does not run a background heartbeat during a blocking socket read;
  an overlong request can lose the lease and must not commit afterward.
- A paused task is not claimed. Pausing does not cancel an already-running task.
  Expired running leases may be reclaimed with a new token, up to three claims
  per run; then the run becomes `error` with `lease_exhausted`. Attempt count
  means claims, not HTTP calls. Earlier tokens cannot renew, record a failure or
  commit results after a newer claim/cancellation.
- A request guard uses a MySQL session `GET_LOCK` keyed by a SHA-256 credential
  fingerprint; the actual key is never stored in the quota table or lock name.
  Different sources sharing the same credential use the same lock and persisted
  quota. Lock acquisition is bounded to ten seconds and fails closed.
  A dedicated connection holds this **advisory** lock through the HTTP response,
  but does not keep an InnoDB transaction open during network I/O.
- The next request timestamp is stored both before and after the call. Wait
  eight database seconds after completion: with second-precision DATETIME this
  guarantees at least seven elapsed seconds, and avoids compressing real call
  gaps if a process pauses between reserving a slot and sending the request.
  Lost DB connections/processes still require real MySQL crash/concurrency
  acceptance; nonparticipating clients and the standalone verifier can bypass
  this gate and must not be run concurrently with it.
- Persisted `request_count` counts guarded attempts reserved before sending,
  including failed connections; a crash between reservation and sending can
  conservatively overcount. This is not a count of successful provider responses.
- The worker makes **one HTTP attempt per claim**. Network/framing failures and
  HTTP 429/502/503/504 requeue the same immutable run with a fresh future attempt.
  The combined normal-retry/crash-recovery limit is three claims. Backoff is
  `60 * 2^(attempt-1)` seconds (capped at 900) plus up to ten seconds of jitter,
  or the provider delay, whichever is greater. Retry state and audit event
  commit atomically; old tokens are cleared and cannot continue writing.
  The source queue does not let newer enabled work overtake its oldest pending
  retry, preventing older results from overwriting newer overlapping windows.
  A paused task is skipped as before. Authentication, certificate, contract,
  invalid configuration and persistence errors are terminal, not blindly retried.
- `Retry-After` integer seconds and timezone-qualified HTTP dates are parsed by
  one helper. The request guard persists the provider cooldown **before releasing
  its credential-wide lock**, so another source cannot bypass it. Long cooldowns
  are deferred rather than slept inside a three-minute run lease. Supported
  provider delays are at most one year; malformed/ambiguous/oversized instructions
  quarantine the quota with `is_blocked=TRUE` for explicit manual review. Provider
  headers and messages are never copied to DB/logs.
  The standalone verifier also uses the parser but does not participate in DB
  quota or durable job retries.
- Fixed error codes/messages are persisted after rollback. No raw exception
  text, request headers or provider messages are printed by the worker. If a
  failure record cannot be committed or the lease has been lost, the process
  exits nonzero; it never overrides a newer/cancelled/done run to report failure.
  An outage may leave `running` until lease recovery; this is not success.
- `run --once` exits `0` for a complete raw result or idle (idle explicitly has
  `completed_run:false`), `2` for partial raw data, `3` when a durable retry was
  scheduled, `1` for terminal failure. The continuous loop reports
  `retry_scheduled` without success or data completeness, waits for future claims,
  and does not refresh heartbeat from that failed attempt. It continues on
  `partial` results; terminal errors exit nonzero for supervision.
  SIGINT/SIGTERM stop further claims after the current synchronous operation.

Inspect the configured credential's shared cooldown without printing its key or
fingerprint. After reviewing a malformed provider delay with the supplier, an
authorized operator can clear only the block (not erase the normal cooldown):

```powershell
python .\worker.py quota
python .\worker.py quota --reviewed-by 1
```

These commands use environment credentials and trusted operator attribution,
not a public API authorization mechanism. Block clearing and its actor audit are
one transaction; an audit failure leaves the block intact. All jobs sharing the
credential can be affected by this review, even across data sources.

[`Dockerfile.worker`](./Dockerfile.worker) and the separate
[worker Compose definition](../others/docker-compose-jishiyun-worker.yml) run a
non-root, read-only-filesystem worker without altering existing MyEMS services.
The Dockerfile default is help; Compose explicitly supplies `run` and restarts
on failure. The current `worker_state` heartbeat describes **active task leases**,
not an idle service-health probe.

### Local loop health

[`health.py`](./health.py) checks a dedicated local heartbeat file, atomically
written **after a complete loop iteration** (including database claim/poll).
Compose runs with `--health-file /tmp/jishiyun-worker-health.json` on a 1 MiB
tmpfs; it does not make the root filesystem writable. A normal local CLI run
does not write shared health files unless `--health-file` is explicitly supplied.
Do not run multiple local workers against the same heartbeat path.

The Dockerfile probes this marker every 30 seconds, with a 60-second start
period, three failed checks and a 240-second freshness threshold. Missing,
malformed, future or stale monotonic timestamps are unhealthy. Startup removes
old markers, normal/failure exit clears them, and abrupt termination becomes
unhealthy after the freshness window. This is a bounded **freshness check**, not
instant process-death detection or an HTTP health endpoint.
`run --once` clears its marker on exit and is not a health-checked service.

A partial data result can have fresh loop health: missing-data alarms remain
in the database and are **not** cleared by a healthy container. A DB/scheduler/
request error does not refresh health. A blocked iteration eventually becomes
unhealthy even if the process is still present. Docker `restart: on-failure`
restarts exited failures, not a running process marked unhealthy; external
monitoring/restart policy for that case remains a deployment responsibility.

To enable scheduling, explicitly override the Compose command to include
`--schedule` while retaining `--health-file` and its path. Compose parsing is
validated, but image build/start, real health transitions and live MySQL
transactions remain unverified. Do not report a production deployment yet.

## Raw-result persistence foundation

[`persistence.py`](./myems_jishiyun/persistence.py) exposes `ResultStore.save(run_id, lease_token,
readings)` for already-parsed `EnergyReading` records. There is no task claiming,
HTTP request, periodic loop or database connection discovery inside this module.
The verifier image remains read-only and does not include this library.

The caller must supply a **dedicated, idle MySQL connection** that can access
both `myems_system_db` and `myems_historical_db` on the **same MySQL server**.
All participating tables, including `tbl_data_sources`, are checked for InnoDB
before persistence. A missing/nontransactional table is an explicit failure.
Do not reuse the connection across threads or an existing transaction.
Different database servers are not supported by this implementation; they
require the previously planned replay/checkpoint mechanism before deployment.

The queued-run producer must capture the approved mapping scope *before*
requesting provider data. It must never build it from the returned device list.
Use `scope_snapshot(tuple_of_scope_meters)` to serialize a version-1 snapshot:

```json
{
  "version": 1,
  "meters": [{
    "address": "synthetic-meter",
    "external_id": 1,
    "effective_start_utc": "2026-10-01T00:00:00+00:00",
    "effective_end_utc": null
  }]
}
```

Scope addresses and external IDs must each be unique. Effective ranges are
`[start, end)` and restrict each meter's expected slots, so later scope changes
cannot erase an older run's expected devices. Run `parameters_json` must also
snapshot an integer `delay_minutes` (normally `40`). Missing/invalid settings
fail explicitly; the store refuses to finalize a window before
`window_end_utc + delay_minutes`, using the database clock.
Persistence windows are limited to one day; the explicit backfill producer
splits longer requests into individual runs.

Persistence behavior:

- Lock the source row, then the run row; all future worker writers must follow
  the same lock order. Require `running`, matching UUID lease and unexpired
  database-clock lease. Recheck lease expiry in the final conditional update.
  Never hold this transaction open while making an HTTP request.
- Reuse the raw-table unique key. Identical register/identity/quality replays
  keep the same revision and first-run provenance. Corrected values or quality
  increment the revision and audit before/after values and source run IDs.
  Raw metadata-only changes do not increment the register revision.
- Conflicting duplicates or mismatched external IDs become quarantined
  `conflict` samples with NULL registers; invalid values remain NULL, never zero.
  Raw records are retained in a `records` array, including duplicate/conflicting
  rows. Decimal raw values are exact strings. Non-finite float tokens are
  explicitly tagged in audit JSON, not emitted as nonstandard JSON numbers.
- Unexpected addresses, inactive slots and end-boundary records are audited,
  count toward a `partial` outcome, and **do not overwrite other raw slots**.
  Boundary acquisition and validation are still a separate future stage.
- Missing slots are not fabricated as raw zero/null rows and do not delete
  previously stored readings. Completeness here describes the **current
  response**, not the union of all previous responses. An already stored reading
  alone cannot recover an alert after a new response reports it missing.
- Create/update unique per-slot alerts immediately after a due result. Repeated
  observations do not duplicate alerts. Acknowledgement does not recover an
  alert. Only a fresh valid reading with the expected identity recovers it.
  Reopening retains the first-discovery history, resets stale acknowledgement
  and audits its prior acknowledgement. Lifecycle events link by `alert_id`.
  A change from missing to invalid/conflict does not imply recovery.
  This raw stage never recovers `boundary_missing` alerts; they belong to the
  future hourly-boundary validator.
- Raw rows, alerts, events and the run's counts/status commit together. On an
  error or expired lease they roll back and propagate failure; the worker
  records failure separately after rollback, subject to the same live token.
  It never treats the exception as successful empty data.
- Lower-ID runs cannot overwrite newer raw samples or newer slot alerts.
  The future scheduler must serialize overlapping runs or issue a new run ID
  when retrying an older overlapping run. Do not mutate source/window/scope
  snapshots of an executing run.

`done` means only that the requested **raw interval slots** were complete.
This module does not mark hourly boundaries or business calculations complete,
update mapping completion watermarks, publish to `tbl_energy_value`, write
`tbl_meter_hourly`, or enqueue downstream recalculations. Revision audit events
mark the need for recalculation, not its execution.

The extension DDL now includes `tbl_jishiyun_runs.unexpected_count`,
`tbl_jishiyun_request_quotas.is_blocked` and the tenth table
`tbl_jishiyun_backfill_batches`. If an earlier
development snapshot was manually installed, compare/migrate its columns before
using this library; re-running `CREATE TABLE IF NOT EXISTS` will not add it.
Before production, verify DDL installation/re-run, real-driver Decimal/datetime
types, cross-schema rollback, lease expiry and competing MySQL connections.

## Additive database foundation

Back up both databases before executing
[`myems_jishiyun.sql`](../database/install/myems_jishiyun.sql) against an existing
MyEMS installation. Use it for both new installations (after base schema setup)
and existing installations. The script creates ten namespaced extension tables
without dropping databases, modifying existing tables or changing MyEMS's version.
An actual MySQL installation/re-run test is required before deployment; SQL review
alone is not database validation.

| Database | Table | Purpose |
|---|---|---|
| system | `tbl_jishiyun_tasks` | Task definitions; enabled only after approval |
| system | `tbl_jishiyun_runs` | Preview/queue/lease/execution state, scope snapshot and coverage counts |
| system | `tbl_jishiyun_run_items` | Per-meter archive preview, approved configuration and initialization result |
| system | `tbl_jishiyun_meter_mappings` | Unique external-to-local meter/point ownership, scope and configuration status |
| system | `tbl_jishiyun_alerts` | Unique per-slot quality alerts, independent acknowledgement and recovery |
| system | `tbl_jishiyun_events` | Configuration changes, scope history, sample revisions and alert lifecycle audit |
| system | `tbl_jishiyun_worker_state` | Active task heartbeat/lease; not idle service health |
| system | `tbl_jishiyun_request_quotas` | Credential-wide persisted request spacing, combined with a MySQL advisory lock |
| system | `tbl_jishiyun_backfill_batches` | Immutable backfill-parent identity, child plan and audited cancellation |
| historical | `tbl_jishiyun_energy_readings` | Idempotent raw 15-minute registers with quality, provenance and revision |

Relations follow the existing codebase's application-managed relationship pattern.
Initialization of each meter, point, association and mapping must be one system
database transaction. Raw sample writes and business writes may span database
servers; use replayable checkpoints, not an assumed distributed transaction.
History must not be cascade-deleted with a task or mapping.

The new keys constrain only these extension tables. They do not retroactively
deduplicate `tbl_energy_value` or `tbl_meter_hourly`. The persistence/normalization
stage must audit their existing duplicates and establish a single writer for
Jishiyun points before writing business values.

Task parameters, events and error messages must be sanitized. Re-running
`CREATE TABLE IF NOT EXISTS` is not schema reconciliation: compare existing
definitions before assuming an older or partially installed schema is compatible.
