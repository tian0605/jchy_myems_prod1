"""Explicit local company-2 pilot topology, preserving every meter and its history."""

import argparse
import json
from uuid import uuid4

from configure_pilot import ADDRESSES
from myems_jishiyun.execution import RunQueue
from myems_jishiyun.worker import database_factory
from myems_jishiyun.dimensions import configuration


def configure(queue, actor, *, apply=False):
    with queue.transaction() as cursor:
        cursor.execute(
            "SELECT id FROM myems_system_db.tbl_spaces WHERE id=2 AND name=%s "
            "AND is_input_counted=0 AND is_output_counted=0 FOR UPDATE", ("测试数据公司",),
        )
        if cursor.fetchone() != (2,):
            raise ValueError("Only the explicitly approved uncounted company-2 test space is supported")
        cursor.execute(
            "SELECT external_address,meter_id,is_enabled FROM myems_system_db.tbl_jishiyun_meter_mappings "
            "WHERE data_source_id=%s ORDER BY meter_id FOR UPDATE", (queue.source_id,),
        )
        mappings = cursor.fetchall()
        active = [row[1] for row in mappings if row[2] == 1]
        if len(active) != 9 or {row[0] for row in mappings if row[2] == 1} != ADDRESSES:
            raise ValueError("The approved nine-meter scope changed")
        existing = configuration(cursor, queue.source_id)
        if existing is not None:
            return {"status": "already_configured", **existing}
        cursor.execute("SELECT id,meter_id FROM myems_system_db.tbl_spaces_meters WHERE space_id=2 FOR UPDATE")
        links = cursor.fetchall()
        if sorted(row[1] for row in links) != sorted(row[1] for row in mappings):
            raise ValueError("Unexpected company-2 associations; do not remove unrelated members")
        for schema in ("myems_energy_db", "myems_billing_db", "myems_carbon_db"):
            cursor.execute("SELECT id FROM " + schema + ".tbl_space_input_category_hourly "
                           "WHERE space_id=2 LIMIT 1 FOR UPDATE")
            if cursor.fetchone() is not None:
                raise ValueError("Existing space aggregates require a separate migration review")
        cursor.execute(
            "SELECT DISTINCT energy_category_id,cost_center_id,product_id FROM myems_system_db.tbl_meters "
            "WHERE id IN (" + ",".join(["%s"] * len(active)) + ") FOR UPDATE", tuple(active),
        )
        metadata = cursor.fetchall()
        if len(metadata) != 1 or metadata[0][:2] != (2, 2):
            raise ValueError("Expected isolated pilot category/cost center")
        category, center, product = metadata[0]
        label = "PILOT TEST Jishiyun source " + str(queue.source_id)
        cursor.execute(
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA='myems_production_db' AND TABLE_NAME='tbl_products'",
        )
        if cursor.fetchone() != ("InnoDB",):
            raise ValueError("Product configuration must use InnoDB")
        cursor.execute("SELECT id FROM myems_production_db.tbl_products WHERE name=%s FOR UPDATE", (label,))
        if product is not None or cursor.fetchall():
            raise ValueError("Unexpected existing product ownership requires review")
        cursor.execute("SELECT id FROM myems_system_db.tbl_equipments WHERE name=%s FOR UPDATE", (label,))
        if cursor.fetchall():
            raise ValueError("Unowned pilot equipment requires review")
        details = {"space_id": 2, "meter_ids": active, "energy_category_id": category,
                   "previous_product_id": product, "previous_space_links": links,
                   "removed_space_link_ids": [row[0] for row in links if row[1] not in active],
                   "double_count_policy": "space sums direct meters; equipment is not counted into space"}
        cursor.execute("SELECT description FROM myems_system_db.tbl_spaces WHERE id=2")
        details["previous_space_description"] = cursor.fetchone()[0]
        if not apply:
            return {"status": "preview", **details}
        cursor.execute(
            "INSERT INTO myems_production_db.tbl_products "
            "(name,uuid,unit_of_measure,tag,standard_product_coefficient) VALUES (%s,%s,'kWh','PILOT TEST',1)",
            (label, str(uuid4())),
        )
        cursor.execute("SELECT LAST_INSERT_ID()")
        product = cursor.fetchone()[0]
        details["product_id"] = product
        cursor.execute("INSERT INTO myems_system_db.tbl_spaces_products (space_id,product_id) VALUES (2,%s)",
                       (product,))
        for meter in active:
            cursor.execute("UPDATE myems_system_db.tbl_meters SET product_id=%s WHERE id=%s", (product, meter))
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_equipments "
            "(name,uuid,is_input_counted,is_output_counted,cost_center_id,description) VALUES (%s,%s,0,0,%s,%s)",
            (label, str(uuid4()), center, "JISHIYUN_PILOT_SOURCE:" + str(queue.source_id)),
        )
        cursor.execute("SELECT LAST_INSERT_ID()")
        equipment_id = cursor.fetchone()[0]
        details["equipment_id"] = equipment_id
        for meter in active:
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_equipments_meters (equipment_id,meter_id,is_output) VALUES (%s,%s,0)",
                (equipment_id, meter),
            )
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_spaces_equipments (space_id,equipment_id) VALUES (2,%s)", (equipment_id,),
        )
        cursor.execute("UPDATE myems_system_db.tbl_spaces SET description=%s WHERE id=2",
                       ("JISHIYUN_PILOT_SOURCE:" + str(queue.source_id),))
        for identity in details["removed_space_link_ids"]:
            cursor.execute("DELETE FROM myems_system_db.tbl_spaces_meters WHERE id=%s AND space_id=2", (identity,))
            if cursor.rowcount != 1:
                raise ValueError("Space association changed")
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_events "
            "(data_source_id,actor_user_id,event_type,details_json) "
            "VALUES (%s,%s,'pilot_dimensions_configured',%s)",
            (queue.source_id, actor, json.dumps(details, sort_keys=True)),
        )
        configuration(cursor, queue.source_id)
        return {"status": "configured", **details}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.actor <= 0:
        parser.error("A positive operator ID is required")
    print(json.dumps(configure(RunQueue(database_factory(), 4), args.actor, apply=args.apply)))
