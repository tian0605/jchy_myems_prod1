# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportDeprecated=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from logging import Logger
from typing import Any, TypedDict, cast

import mysql.connector
import paho.mqtt.client as mqtt

import config


SUPPORTED_OBJECT_TYPES = {
    'ANALOG_VALUE': ('tbl_analog_value', 'tbl_analog_value_latest'),
    'ENERGY_VALUE': ('tbl_energy_value', 'tbl_energy_value_latest'),
    'DIGITAL_VALUE': ('tbl_digital_value', 'tbl_digital_value_latest'),
}


SYSTEM_DB_CONFIG = cast(dict[str, Any], config.myems_system_db)
HISTORICAL_DB_CONFIG = cast(dict[str, Any], config.myems_historical_db)


class PointInfo(TypedDict):
    point_id: int
    object_type: str
    is_trend: bool


class PointValue(TypedDict):
    point_id: int
    object_type: str
    is_trend: bool
    utc_date_time: datetime
    value: float | int


class MQTTUserData(TypedDict):
    logger: Logger
    data_source_id: int
    point_map: dict[int, PointInfo]
    broker_config: dict[str, Any]


def process(logger: Logger, data_source_id: int, broker_config: dict[str, Any]) -> None:
    try:
        _update_process_id(data_source_id)
    except Exception as exc:
        logger.error('Error updating process id for data source %s: %s', data_source_id, exc)
        return

    try:
        point_map = _load_points(data_source_id)
    except Exception as exc:
        logger.error('Error loading points for data source %s: %s', data_source_id, exc)
        return

    if not point_map:
        logger.error('Point Not Found in Data Source (ID = %s), acquisition process terminated', data_source_id)
        return

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=_build_client_id(data_source_id, broker_config),
        clean_session=None,
        userdata=cast(
            Any,
            {'logger': logger, 'data_source_id': data_source_id, 'point_map': point_map, 'broker_config': broker_config},
        ),
        protocol=mqtt.MQTTv5,
        transport='tcp',
        reconnect_on_failure=True,
        manual_ack=False,
    )

    username = broker_config.get('username') or broker_config.get('user')
    password = broker_config.get('password', '')
    if username:
        client.username_pw_set(username, password)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message

    keepalive = broker_config.get('keepalive', 60)
    try:
        client.connect_async(broker_config['host'], broker_config['port'], keepalive)
        client.loop_start()
    except Exception as exc:
        logger.error('MQTT connection error for data source %s: %s', data_source_id, exc)
        return

    try:
        while True:
            time.sleep(60)
    finally:
        client.loop_stop()
        client.disconnect()


def _build_client_id(data_source_id: int, broker_config: dict[str, Any]) -> str:
    client_id = broker_config.get('client_id')
    if client_id:
        return client_id
    return 'MYEMS-MQTT-{0}-{1}'.format(data_source_id, os.getpid())


def _on_connect(client: Any, userdata: Any, connect_flags: Any, reason_code: int, properties: Any) -> None:
    mqtt_userdata = cast(MQTTUserData, userdata)
    logger = mqtt_userdata['logger']
    if reason_code != 0:
        logger.error('Bad MQTT connection for data source %s, reason code=%s', mqtt_userdata['data_source_id'], reason_code)
        return

    for topic, qos in _normalize_topics(mqtt_userdata['broker_config']):
        client.subscribe(topic, qos=qos)
        logger.info('Subscribed data source %s to topic %s with qos=%s', mqtt_userdata['data_source_id'], topic, qos)


def _on_disconnect(client: Any, userdata: Any, disconnect_flags: Any, reason_code: int, properties: Any) -> None:
    mqtt_userdata = cast(MQTTUserData, userdata)
    mqtt_userdata['logger'].warning('MQTT client disconnected for data source %s, reason=%s', mqtt_userdata['data_source_id'], reason_code)


def _on_message(client: Any, userdata: Any, message: Any) -> None:
    mqtt_userdata = cast(MQTTUserData, userdata)
    logger = mqtt_userdata['logger']
    data_source_id = mqtt_userdata['data_source_id']
    point_map = mqtt_userdata['point_map']
    try:
        payload_text = message.payload.decode('utf-8')
        payload = json.loads(payload_text)
    except Exception as exc:
        logger.error('Invalid MQTT payload for data source %s on topic %s: %s', data_source_id, message.topic, exc)
        return

    payload_items = payload if isinstance(payload, list) else [payload]
    for payload_item in payload_items:
        try:
            point_value = _normalize_payload_item(data_source_id, payload_item, point_map)
            _persist_point_value(point_value)
        except Exception as exc:
            logger.error('Failed to process MQTT payload for data source %s on topic %s: %s', data_source_id, message.topic, exc)


def _update_process_id(data_source_id: int) -> None:
    cnx = mysql.connector.connect(**SYSTEM_DB_CONFIG)
    cursor = cnx.cursor()
    try:
        cursor.execute(
            ' UPDATE tbl_data_sources SET process_id = %s WHERE id = %s ',
            (os.getpid(), data_source_id),
        )
        cnx.commit()
    finally:
        cursor.close()
        cnx.close()


def _load_points(data_source_id: int) -> dict[int, PointInfo]:
    cnx = mysql.connector.connect(**SYSTEM_DB_CONFIG)
    cursor = cnx.cursor()
    try:
        cursor.execute(
            ' SELECT id, object_type, is_trend '
            ' FROM tbl_points '
            ' WHERE data_source_id = %s AND is_virtual = 0 '
            ' ORDER BY id ',
            (data_source_id,),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        cnx.close()

    point_map: dict[int, PointInfo] = {}
    for row in rows:
        point_row = cast(tuple[Any, Any, Any], row)
        point_id = int(point_row[0])
        object_type = str(point_row[1])
        is_trend = bool(point_row[2])
        point_map[point_id] = {
            'point_id': point_id,
            'object_type': object_type,
            'is_trend': is_trend,
        }
    return point_map


def _normalize_topics(broker_config: dict[str, Any]) -> list[tuple[str, int]]:
    topics = broker_config.get('topics')
    if topics is None:
        topic = broker_config.get('topic')
        if isinstance(topic, list):
            topics = topic
        elif topic:
            topics = [topic]
        else:
            raise ValueError('MQTT topic is required')

    normalized_topics = []
    default_qos = broker_config.get('qos', 0)
    for topic in topics:
        if isinstance(topic, dict):
            name = topic.get('name') or topic.get('topic')
            qos = topic.get('qos', default_qos)
        else:
            name = topic
            qos = default_qos
        if not name:
            raise ValueError('MQTT topic is required')
        normalized_topics.append((name, int(qos)))
    return normalized_topics


def _normalize_payload_item(data_source_id: int, payload_item: dict[str, Any], point_map: dict[int, PointInfo]) -> PointValue:
    if not isinstance(payload_item, dict):
        raise ValueError('Payload item must be a JSON object')

    payload_data_source_id = payload_item.get('data_source_id')
    if payload_data_source_id is not None and int(payload_data_source_id) != int(data_source_id):
        raise ValueError('Payload data_source_id does not match the subscribed data source')

    point_id = int(payload_item['point_id'])
    if point_id not in point_map:
        raise ValueError('Point {0} is not configured for this data source'.format(point_id))

    point = point_map[point_id]
    if point['object_type'] not in SUPPORTED_OBJECT_TYPES:
        raise ValueError('Unsupported object type {0}'.format(point['object_type']))

    utc_date_time = payload_item.get('utc_date_time')
    if utc_date_time:
        timestamp = datetime.fromisoformat(utc_date_time.replace('Z', '+00:00'))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        timestamp = datetime.utcnow()

    value = payload_item['value']
    if point['object_type'] == 'DIGITAL_VALUE':
        actual_value = int(value)
    else:
        actual_value = float(value)

    return {
        'point_id': point_id,
        'object_type': point['object_type'],
        'is_trend': point['is_trend'],
        'utc_date_time': timestamp,
        'value': actual_value,
    }


def _persist_point_value(point_value: PointValue) -> None:
    trend_table, latest_table = SUPPORTED_OBJECT_TYPES[point_value['object_type']]
    cnx = mysql.connector.connect(**HISTORICAL_DB_CONFIG)
    cursor = cnx.cursor()
    try:
        if point_value['is_trend']:
            cursor.execute(
                ' INSERT INTO {0} (point_id, utc_date_time, actual_value) VALUES (%s, %s, %s) '.format(trend_table),
                (point_value['point_id'], point_value['utc_date_time'].isoformat(), point_value['value']),
            )

        cursor.execute(
            ' DELETE FROM {0} WHERE point_id = %s '.format(latest_table),
            (point_value['point_id'],),
        )
        cursor.execute(
            ' INSERT INTO {0} (point_id, utc_date_time, actual_value) VALUES (%s, %s, %s) '.format(latest_table),
            (point_value['point_id'], point_value['utc_date_time'].isoformat(), point_value['value']),
        )
        cnx.commit()
    finally:
        cursor.close()
        cnx.close()