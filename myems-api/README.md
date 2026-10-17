# MyEMS API Service

## Introduction

RESTful API service for [MyEMS](https://github.com/MyEMS/myems) components and third party applications.

### Acquisition raw results and processing phases

Acquisition `runs` now expose the existing `started_datetime_utc` and
`finished_datetime_utc` lifecycle timestamps separately from reading-window
boundaries. Start is the first claim (retained across retries); finish is the
stored completion/termination timestamp and may belong to a failed, partial or
cancelled run. No historical timestamp is fabricated when a field is null.
Task lists add `last_success_finished_datetime_utc` (MAX finish for status done),
`latest_run_id`, `latest_run_status` and `latest_run_finished_datetime_utc`.
Latest means greatest submitted run ID, not last data-window end. All lookups
match both task ID and source ID, in the same read transaction; a newer failure
does not replace last success. Neither timestamp proves downstream completion.

Administrator-only `GET /datasources/{id}/acquisition/readings` lists the selected
source's current raw electric-quantity records across regular collection and
historical backfill. It uses descending raw-record-ID keyset pagination
(`limit` defaults to 50, maximum 200; `before_id`), not a single run window.
Current meter names are joined through the source/address/external-ID mapping;
unmapped and paused historical readings remain visible. No provider calls,
payloads, credentials, missing-as-zero rows or duplicate copies per response.

Alongside `quality`, the response supplies `energy_status`, `settlement_status`,
`dimensions_status` and structured `processing_details` for the related hours.
These are **read-only derived fields**, not independently mutable flags in the
raw table: persisted audit/proof chains, current raw revisions and actual target
rows remain the authority, including approved correction proofs. This avoids
stale "processed" flags after a raw revision, target deletion or correction.
The source row lock gives each response a consistent view against cooperative
writers. Database errors and invalid stored audit/timestamp contracts fail
explicitly rather than returning success-shaped status defaults.

An hour-boundary reading participates in the preceding and following hour;
mixed completion is `partial`. Other states distinguish `processed`, `pending`,
`queued`, `running`, `blocked`, `error`, `review_required` and `not_applicable`.
Phase completion requires matching audit **and** business rows, not a run's done
flag or `last_run_id`. A paused mapping does not erase proven historical energy
completion. Notes carry the UTC hour and fixed diagnostic/gate codes; the Admin
renders UTC+8 with localized labels. Notes are system diagnostics, not editable
operator remarks or approval/retry controls.

`first_run_id` is the initial inserting run; `last_run_id` is the last saving or
verifying run, including unchanged duplicate replay. It does **not** necessarily
identify the last value change. `revision` changes with the stored identity,
value or quality; raw quality `valid/invalid/conflict` is separate from downstream
processing. Existing run-window detail remains available and unchanged.

### Jishiyun corrected space reports

The space energy-category, cost and carbon reports bypass Redis reads and writes
when the selected space has a directly associated `JISHIYUN_REGISTER` meter,
just as the staged meter reports do. Ordinary spaces retain caching. This keeps
approved pilot corrections visible without flushing unrelated cache keys;
authentication, enterprise visibility and product-binding checks remain intact.
Space product reports use their existing direct-product-meter calculation,
whereas equipment category reports use materialized equipment hourly tables.
The local company-2 acceptance independently verified both paths and their XLSX
exports; no live browser-login acceptance is implied.

## Dependencies

anytree

simplejson

mysql-connector-python

falcon

falcon_cors

falcon-multipart

gunicorn

et_xmlfile

jdcal

openpyxl

pillow

python-decouple

paho-mqtt

plotly

kaleido

requests

redis


## Quick Run for Development

The acquisition administrator endpoints use the shared local `myems-jishiyun`
package. Install it into the **same interpreter** as this service before starting:

```powershell
# From the repository root, with the selected virtual environment active:
python -m pip install -e .\myems-jishiyun
```

API Docker builds now require the repository root as build context so the same
package is installed in the image (no sibling-directory `sys.path` injection):

```powershell
docker build -f .\myems-api\Dockerfile -t myems-api .
```

The Windows/Linux Compose build definitions and release image workflow use this
context. Existing prebuilt images must be rebuilt to provide the new endpoints.

Quick run on Linux (NOT for production use):
```bash
cd myems/myems-api
sudo pip install -r requirements.txt
cp example.env .env
sudo chmod +x run.sh
./run.sh
```

How to run on Windows (NOT for production usage):

Install python 3.10, 3.11, or 3.12

Find python path in Command Prompt:
```bash
where python
```
Assume the result is 'C:\Users\johnson\AppData\Local\Programs\Python\Python310\python.exe'

Copy fcntl.py and pwd.py to lib folder:
```bash
copy myems\myems-api\fcntl.py C:\Users\johnson\AppData\Local\Programs\Python\Python310\Lib
copy myems\myems-api\pwd.py C:\Users\johnson\AppData\Local\Programs\Python\Python310\Lib
```

Install and run with waitress:
```bash
pip install waitress
cd myems\myems-api
waitress-serve --listen=0.0.0.0:8000 app:api
```


## Installation

### Installation Option 1: Install myems-api on Docker

Refer to [myems.cn](https://myems.cn/docs/installation/docker-linux#step-2-myems-api)

### Option 2: Online install myems-api on Ubuntu Server with internet access

Refer to [myems.cn](https://myems.cn/docs/installation/debian-ubuntu#step-2-myems-api)

### Option 3: Offline install myems-api on Ubuntu Server without internet access

In this section, you will offline install myems-api on Ubuntu Server without internet access.
* Download tools 
```bash
mkdir ~tools && cd ~/tools
git clone https://github.com/c0fec0de/anytree.git
git clone https://github.com/simplejson/simplejson.git
wget https://cdn.mysql.com//Downloads/Connector-Python/mysql-connector-python-8.0.28.tar.gz
mkdir ~/tools/falcon && cd ~/tools/falcon
pip download cython falcon falcon-cors falcon-multipart
cd ~/tools
mkdir ~/tools/gunicorn && cd ~/tools/gunicorn
pip download gunicorn
cd ~/tools
wget https://foss.heptapod.net/openpyxl/et_xmlfile/-/archive/1.1/et_xmlfile-1.1.tar.gz
cd ~/tools
git clone https://github.com/phn/jdcal.git
mkdir ~/tools/pillow && cd ~/tools/pillow 
pip download Pillow
cd ~/tools
wget https://foss.heptapod.net/openpyxl/openpyxl/-/archive/3.0.7/openpyxl-3.0.7.tar.gz
cd ~/tools
git clone https://github.com/henriquebastos/python-decouple.git
```
* Copy source code and tools to the production Ubuntu Server and then run:
```bash
cd ~/tools/anytree
python setup.py install 
cd ~/tools/simplejson
python setup.py install 
cd ~/tools
tar xzf mysql-connector-python-8.0.28.tar.gz
cd ~/tools/mysql-connector-python-8.0.28
python setup.py install
export LC_ALL="en_US.UTF-8"
export LC_CTYPE="en_US.UTF-8"
dpkg-reconfigure locales
pip install --upgrade --no-index --find-links ~/tools/falcon cython falcon falcon-cors falcon-multipart
pip install --no-index --find-links ~/tools/gunicorn gunicorn
cd ~/tools
tar xzf et_xmlfile-1.1.tar.gz
cd ~/tools/et_xmlfile-1.1
python setup.py install
cd ~/tools/jdcal
python setup.py install
cd ~/tools
pip install --no-index --find-links ~/tools/pillow Pillow
tar xzf openpyxl-3.0.7.tar.gz
cd ~/tools/openpyxl-3.0.7
python setup.py install
cd ~/tools/python-decouple
python setup.py install
```

* Install  myems-api service:
```bash
cp -r myems/myems-api /myems-api
```
Create .env file based on example.env and edit the .env file if needed:
```bash
cp /myems-api/example.env /myems-api/.env
nano /myems-api/.env
```
Check or change the listening port (default is 8000) in myems-api.service and myems-api.socket:
```bash
nano /myems-api/myems-api.service
```
```bash
ExecStart=/usr/local/bin/gunicorn -b 0.0.0.0:8000 --pid /run/myems-api/pid --timeout 600 --workers=4 app:api
```
```bash
nano /myems-api/myems-api.socket
```
```bash
ListenStream=0.0.0.0:8000
```
Add port to firewall:
```bash
ufw allow 8000
```
Setup systemd configure files:
```bash
cp /myems-api/myems-api.service /lib/systemd/system/
cp /myems-api/myems-api.socket /lib/systemd/system/
cp /myems-api/myems-api.conf /usr/lib/tmpfiles.d/
```
   Next enable the services so that they autostart at boot:
```bash
  systemctl enable myems-api.socket
  systemctl enable myems-api.service
```
Start the services :
```bash
systemctl start myems-api.socket
systemctl start myems-api.service
```

### Installation Option 4: Install myems-api on macOS

Refer to [Installation on macOS (Chinese)](/myems-api/installation_macos_zh.md)


## API List

Please refer to [API List](https://myems.cn/docs/api)

## References

[1]. http://myems.cn

[2]. https://falconframework.org/

[3]. https://github.com/lwcolton/falcon-cors

[4]. https://github.com/yohanboniface/falcon-multipart

[5]. http://gunicorn.org

[6]. https://github.com/henriquebastos/python-decouple/

[7]. https://foss.heptapod.net/openpyxl/openpyxl

[8]. https://foss.heptapod.net/openpyxl/et_xmlfile/

[9]. https://docs.pylonsproject.org/projects/waitress/en/latest/
# Jishiyun mapping batch pause

The existing administrator-only
`POST /datasources/{id}/acquisition/mapping-batch` accepts `pause_raw` in addition
to `edit_range` and `enable_raw`. Pause uses the same envelope as enable:
1–200 unique `items` containing `id` and `expected_configuration`,
`confirm_kwh_direct: true`, and null `collection_start_utc`/`collection_end_utc`.
It reuses single-mapping ownership, unit, optimistic snapshot, open-run and
uncancelled-backfill guards. All changed mappings and per-mapping audit events
commit in one transaction or none; repeated already-paused requests are no-ops.
Historical values, effective ranges and staged point types remain unchanged.
The workbench renders timestamps in UTC+8; this does not change UTC API response
values or the offset-aware input contract for other clients.
