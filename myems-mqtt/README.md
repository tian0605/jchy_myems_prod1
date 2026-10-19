## MyEMS MQTT Service

### Introduction

This service subscribes to MQTT topics and writes telemetry into MyEMS historical tables.

### Supported local payload

```json
{
  "data_source_id": 10001,
  "point_id": 10001,
  "utc_date_time": "2026-04-26T12:00:00",
  "value": 42.5
}
```

### Connection JSON example for datasource

```json
{
  "host": "127.0.0.1",
  "port": 1883,
  "topic": "testtopic",
  "qos": 0
}
```

### Quick run for development

```bash
cd myems-mqtt
pip install -r requirements.txt
cp example.env .env
python main.py
```

### Callback regression tests

The service uses Paho MQTT Callback API VERSION2 for its MQTTv5 connect and
disconnect callbacks. Explicitly selecting this API prevents a disconnect from
terminating the network thread with a callback argument mismatch.

Run the offline tests after installing the existing requirements:

```bash
cd myems-mqtt
python -m unittest test_acquisition_callbacks -v
```

The tests dispatch successful and rejected MQTTv5 connection acknowledgements
and a disconnect through Paho itself. Database writes and network connections
are mocked; no telemetry is published.

Broker authentication is separate from callback compatibility. A successful
subscription does not prove that devices are publishing new data. Verify broker
message counters and historical timestamps during deployment acceptance.

### Notes

- The first implementation supports one or more configured topics per datasource.
- Payloads may be a single JSON object or a JSON array of objects.
- `point_id` must belong to the datasource configured in MyEMS.