"""Keep staged acquisition reports fresh while hourly data is being accepted."""

import mysql.connector

import config


def meter_report_cache_allowed(meter_id, meter_uuid):
    connection = mysql.connector.connect(**config.myems_system_db)
    cursor = None
    try:
        cursor = connection.cursor()
        identity = "m.id" if meter_id is not None else "m.uuid"
        cursor.execute(
            "SELECT p.id FROM tbl_meters m "
            "JOIN tbl_meters_points mp ON mp.meter_id=m.id "
            "JOIN tbl_points p ON p.id=mp.point_id "
            "WHERE " + identity + "=%s AND p.object_type='JISHIYUN_REGISTER' LIMIT 1",
            (meter_id if meter_id is not None else meter_uuid,),
        )
        return cursor.fetchone() is None
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


def space_report_cache_allowed(space_id, space_uuid):
    connection = mysql.connector.connect(**config.myems_system_db)
    cursor = None
    try:
        cursor = connection.cursor()
        identity = "s.id" if space_id is not None else "s.uuid"
        cursor.execute(
            "SELECT p.id FROM tbl_spaces s "
            "JOIN tbl_spaces_meters sm ON sm.space_id=s.id "
            "JOIN tbl_meters_points mp ON mp.meter_id=sm.meter_id "
            "JOIN tbl_points p ON p.id=mp.point_id "
            "WHERE " + identity + "=%s AND p.object_type='JISHIYUN_REGISTER' LIMIT 1",
            (space_id if space_id is not None else space_uuid,),
        )
        return cursor.fetchone() is None
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()
