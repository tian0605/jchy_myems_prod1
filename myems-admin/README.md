# myems-admin

## Introduction

系统管理用户界面，用于MyEMS系统配置

Admin UI for MyEMS system settings

## Jishiyun acquisition workbench (in development)

Execution records distinguish **data-window start/end** from actual **execution
start** and **completion/termination**. Task lists show last successful completion,
latest run ID/status and latest completion/termination, all UTC+8. Failed/partial
runs do not advance last success; queued/running or never-run tasks can show `-`.
Start retains the first claim across retries. These fields reuse persisted
lifecycle timestamps without changing worker behavior or inventing missing
history. A notice explains that collection completion is not downstream completion.

Chinese UI labels include raw quality (valid/invalid/conflict display as
有效/无效/冲突), phase columns and processing notes. The raw-results table has a
quality guide plus descriptions for all nine processing states above it.
English, Simplified Chinese and Traditional Chinese bundles are versioned in
the entry HTML so a newly loaded page does not combine a new template with
cached old translations. Nginx revalidates the entry HTML with `Cache-Control:
no-cache`; already-open pages still require reload. Unknown quality codes
remain visible instead of being silently relabelled as valid.

The view selector now includes **Acquisition interface results** for the selected
source's regular and historical-backfill raw records across all windows, with
current meter name, address, external ID, UTC+8 reading time, exact cumulative
kWh, quality, revision and first/last writer/verifier run IDs. Pagination uses
record ID, not reading time. Unmapped names display `-`; no filtering by enabled
mapping removes historical records.

Independent hourly-energy, billing/carbon and space/equipment-category status
columns are verified server-side from current raw revisions, effective audit
proofs and target rows. They are read-only computed fields, not raw quality or
operator-maintained flags. A boundary's two adjacent hours are summarized as
partial if only one is complete. Expand **Processing notes** for each hour and
diagnostic reason, rendered in UTC+8 even outside China. Notes do not mutate,
approve or retry processing. A later unchanged raw replay can change last writer
without changing revision; the former "Last changed run" heading now correctly
reads "Last writer/verifier run".

Run detail now distinguishes initialization from energy runs by the stored window.
Initialization retains per-meter object outcomes. Energy detail shows current
source-window raw kWh registers, timestamps, quality, revision and first/last
writer runs, not archive fields. This is explicitly not an immutable per-response
snapshot or exact run-scope summary: later backfill can change current rows.
The original run counters remain authoritative for that response's completeness.
Missing samples are not displayed as zero registers; business readiness still
requires complete hourly boundaries.

The Data source settings page includes a **Jishiyun acquisition** tab for
administrator-only task/run/alert/event inspection, per-meter results, task
pause/resume, run cancellation and independent alert acknowledgement. It requires
the matching API and extension schema; failures are displayed explicitly.
The same tab now supports archive fetch submission, safe paged preview,
cross-page selection, batch configuration approval and initialization submission
for an existing enabled initialization task. New mappings remain disabled.
Task creation/editing now supports initialization, regular collection and
backfill task definitions. New tasks are paused; changes require confirmation,
an expected snapshot and no open runs. Used execution settings are immutable
(create a new task); task type cannot be changed. The mapping view now provides
exclusive staged meter/point ownership verification and a collection-range editor
with fixed Beijing-time (UTC+8) datetime pickers, 15-minute validation,
snapshot confirmation and an audit. All workbench timestamp lists, nested details,
confirmation previews and mapping/backfill pickers use Beijing time (UTC+8).
Angular model options fix picker timezone independently of the browser; requests
carry explicit +08:00 offsets. Original UTC snapshots are not mutated, API
technical `_utc` keys and database storage are unchanged. Task timezone is fixed
to 480 minutes in the UI; an existing different timezone is explicitly rejected
on save rather than silently rewritten. Duration/minute-count settings remain
numeric. Selected mappings
can be reviewed across pages (at most 200), with atomic batch range writes or
explicit direct-kWh raw enablement or pause. Any rejected item rolls back the entire
batch. Successful batches clear selection; reselect before the next action.
Batch enablement/pause use saved ranges, not unsaved picker values.
Batch range review, raw enablement and raw pause have adjacent independent toolbar entries.
The raw entry opens only the direct-kWh confirmation panel; no range form is required.
Batch pause preserves historical data and ranges, keeps ownership, kWh, stale
snapshot, open-run and retained-backfill guards, and audits each changed mapping.
It does not automatically cancel business jobs. After an uncertain backfill
response, retry retains the exact submitted identity and window even if local
picker state changes; HTTP input rejection alone unlocks correction.
Enabled/conflicted mappings and sources with open runs or energy history cannot
change ranges. Saving never enables a mapping or marks it ready. A separate
confirmed raw-collection control requires an existing range, exact kWh unit,
direct-value confirmation, verified ownership and an expected snapshot. It marks
pending mappings ready/enabled or pauses them without changing staged point types
or activating business normalization. Open source runs and all uncancelled
backfill parents block changes; cancel retained parents before changing scope
(committed data remains). End-dated disabled mappings remain eligible for
historical backfill. Business activation, correction/rebinding and initialization
cache invalidation remain unfinished. Updated API/Admin are deployed locally.
Batch range UTC+8 conversion, replay, rollback and actor audit passed real
MySQL/HTTP checks in a disposable source, without provider calls. Browser
visual acceptance remains pending because the browser tool did not respond.
Enabled backfill tasks now provide a confirmed range submission form (at most
31 days, one-day shards). The **Backfill batches** view lists durable parents
and live progress, resumes interrupted submissions under the original actor,
and cancels batches with child-lease fencing. Sent request identities/windows
remain locked for uncertain retries; inspect/resume/cancel the parent rather
than create duplicate identities. Input-validation failures before parent
creation allow corrections. Parent-only history also freezes task execution
settings and mapping ranges. These operations do not activate business processing.
See the [acquisition service README](../myems-jishiyun/README.md#administrator-monitoringcontrol-workbench)
for endpoint, permission and transaction semantics.

Run dependency-free controller/localization tests with
`node --test myems-admin/test-acquisition.js` from the repository root.
The isolated local container stack has passed implemented-path browser checks
(task pause/resume, alert acknowledgement and 50+20 item pagination).
The mapping list/detail and history-freeze error have passed browser checks.
Successful range edits/replay and ownership/conflict guards passed real MySQL
HTTP tests in a disposable synthetic source; that fixture was removed and the
retained 70 mappings remain disabled. A successful fresh-source form save has
not been separately browser-verified.
Backfill form confirmation, visible empty-scope failure, draft locking, parent
progress and cancellation passed browser checks. Real MySQL HTTP prefix
resume/replay/cancel/old-lease rejection passed in a disposable source that was
removed afterward. Cancelled synthetic batch 2 remains with zero children.
The task editor has also passed real browser create/edit/confirmation checks;
the two synthetic probe tasks remain paused. Real provider and full-version
business acceptance remain pending; see the linked
service README for scope and local login details.

## Prerequisites

nginx-1.18.0 or later


## Installation

### Option 1: Install myems-admin on Docker

Refer to [myems.cn](https://myems.cn/docs/installation/docker-linux#step-3-myems-admin)

## Option 2: Install on NGINX Server

Refer to [myems.cn](https://myems.cn/docs/installation/debian-ubuntu#step-3-myems-admin)

## Option 3: Install on Apache2 Server
* Install Apache2 Server

refer to https://httpd.apache.org/docs/2.4/install.html

* Configure Apache2
```bash
  sudo vi /etc/apache2/ports.conf
```
Add a Listen
```
Listen 8001
```
```bash
sudo vi /etc/apache2/sites-available/000-default.conf
```
Add a new 'VirtualHost' as below
```
<VirtualHost 127.0.0.1:8001>
        ServerAdmin MyEMS-admin
        DocumentRoot /var/www/myems-admin
        
        <Directory "var/www/myems-admin">
                Options FollowSymLinks
                AllowOverride All
                Require all granted
    			Header set Access-Control-Allow-Origin *
        </Directory>
        ErrorLog ${APACHE_LOG_DIR}/error.log
        CustomLog ${APACHE_LOG_DIR}/access.log combined
        ProxyRequests Off
		ProxyPreserveHost On
		
		<Proxy *>
			Order Deny,Allow
			Allow from all
		</Proxy>
		ProxyPass /api http://127.0.0.1:8000/
		ProxyPassReverse /api http://127.0.0.1:8000/
</VirtualHost>
```

* Install myems-admin :
  If the server can not connect to the internet, please compress the myems/myems-admin folder and upload it to the server and extract it to ~/myems/myems-admin
```bash
sudo cp -r myems/myems-admin  /var/www/myems-admin
sudo chmod 0755 -R /var/www/myems-admin
```
  Check the config file and change it if necessary:
```bash
sudo nano /var/www/myems-admin/app/api.js
```


## References

[1]. https://myems.cn

[2]. https://dev.mysql.com/doc/connector-python/en/ 

[3]. https://nginx.org/