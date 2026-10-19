"""Real MySQL correction rehearsal on temporary copies; never edits supplier readings."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from myems_jishiyun.correction import correct_run
from myems_jishiyun.dimensions import aggregate_dimensions
from myems_jishiyun.execution import RunQueue
from myems_jishiyun.normalization import BusinessGateError, normalize_hour
from myems_jishiyun.settlement import settle_run
from myems_jishiyun.worker import database_factory


SYSTEM_TABLES = (
    "tbl_data_sources", "tbl_jishiyun_tasks", "tbl_jishiyun_runs", "tbl_jishiyun_events",
    "tbl_jishiyun_meter_mappings", "tbl_jishiyun_worker_state", "tbl_jishiyun_request_quotas",
    "tbl_jishiyun_business_jobs", "tbl_jishiyun_alerts", "tbl_meters", "tbl_points", "tbl_meters_points",
    "tbl_energy_categories", "tbl_cost_centers", "tbl_tariffs", "tbl_tariffs_timeofuses",
    "tbl_cost_centers_tariffs", "tbl_spaces", "tbl_equipments", "tbl_spaces_meters", "tbl_equipments_meters",
    "tbl_spaces_virtual_meters", "tbl_spaces_offline_meters", "tbl_spaces_equipments",
    "tbl_spaces_combined_equipments", "tbl_spaces_shopfloors", "tbl_spaces_stores", "tbl_spaces_tenants",
    "tbl_equipments_virtual_meters", "tbl_equipments_offline_meters",
    "tbl_variables", "tbl_combined_equipments_meters", "tbl_shopfloors_meters", "tbl_stores_meters", "tbl_tenants_meters",
)


class CloneCursor:
    def __init__(self, cursor, mapping, failure):
        self.cursor, self.mapping, self.failure = cursor, mapping, failure

    def execute(self, sql, params=()):
        for original, clone in self.mapping.items():
            sql = sql.replace(original, clone)
        params = tuple(self.mapping.get(value, value) if isinstance(value, str) else value for value in params)
        if self.failure() and "INSERT INTO" in sql and "'business_correction_committed'" in sql:
            raise RuntimeError("Synthetic correction audit write failure")
        return self.cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class CloneConnection:
    def __init__(self, connection, mapping, failure):
        self.connection, self.mapping, self.failure = connection, mapping, failure

    def cursor(self):
        return CloneCursor(self.connection.cursor(), self.mapping, self.failure)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def main():
    connect = database_factory()
    token = uuid4().hex[:12]
    tables = {"myems_system_db": SYSTEM_TABLES,
              "myems_historical_db": ("tbl_jishiyun_energy_readings", "tbl_energy_value")}
    for name in ("energy", "billing", "carbon"):
        tables["myems_" + name + "_db"] = (
            "tbl_meter_hourly", "tbl_space_input_category_hourly", "tbl_equipment_input_category_hourly",
        )
    mapping = {schema: "jishiyun_accept_" + token + "_" + schema[6:-3] for schema in tables}
    created = []
    ddl = connect()
    cursor = ddl.cursor()
    for schema in ("myems_energy_db", "myems_billing_db", "myems_carbon_db"):
        optional = []
        for kind in ("space", "equipment"):
            for suffix in ("input_item_hourly", "output_category_hourly"):
                table = "tbl_" + kind + "_" + suffix
                cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (schema, table))
                if cursor.fetchone() is not None:
                    optional.append(table)
        tables[schema] += tuple(optional)
    failure = [False]
    queue = RunQueue(lambda: CloneConnection(connect(), mapping, lambda: failure[0]), 4)
    original_queue = RunQueue(connect, 4)
    slot = (4, "322212000048", datetime(2026, 10, 16, 16))

    def signature(q):
        with q.transaction() as c:
            c.execute(
                "SELECT forward_active_register,revision FROM myems_historical_db.tbl_jishiyun_energy_readings "
                "WHERE data_source_id=%s AND external_address=%s AND reading_datetime_utc=%s", slot,
            )
            return c.fetchone()

    def revise(value, revision):
        with queue.transaction() as c:
            c.execute(
                "UPDATE myems_historical_db.tbl_jishiyun_energy_readings "
                "SET forward_active_register=%s,revision=%s "
                "WHERE data_source_id=%s AND external_address=%s AND reading_datetime_utc=%s",
                (value, revision) + slot,
            )
            assert c.rowcount == 1

    try:
        for schema, clone in mapping.items():
            cursor.execute("CREATE DATABASE `" + clone + "`")
            created.append(clone)
            for table in tables[schema]:
                cursor.execute("CREATE TABLE `" + clone + "`.`" + table + "` LIKE `" + schema + "`.`" + table + "`")
        with original_queue.transaction() as c:
            for schema, clone in mapping.items():
                for table in tables[schema]:
                    c.execute("INSERT INTO `" + clone + "`.`" + table + "` SELECT * FROM `" + schema + "`.`" + table + "`")
        original = signature(original_queue)
        assert correct_run(queue, 31, 1)["status"] == "no_changes"
        revised = original[0] + Decimal("0.25")
        revise(revised, original[1] + 1)
        preview = correct_run(queue, 31, 1)
        assert len(preview["affected_hours"]) == 2
        assert len([w for w in preview["writes"] if "_input_category_hourly" in w["table"]]) == 12
        expected_deltas = {"myems_energy_db": Decimal("0.25"), "myems_billing_db": Decimal("0.575"),
                           "myems_carbon_db": Decimal("0.375")}
        changed_business_rows = 0
        for write in preview["writes"]:
            if write["register"]:
                continue
            magnitude = expected_deltas[write["table"].split(".")[0]]
            expected_delta = magnitude if write["timestamp_utc"].startswith("2026-10-16T15:") else -magnitude
            assert Decimal(write["after"]) - Decimal(write["before"]) == expected_delta
            changed_business_rows += 1
        assert changed_business_rows == 18
        digest = preview["approval_hash"]
        revise(revised + Decimal("0.125"), original[1] + 2)
        try:
            correct_run(queue, 31, 1, expected_hash=digest, apply=True)
        except BusinessGateError as exc:
            assert str(exc) == "correction_preview_stale_or_approval_hash_missing"
        else:
            raise AssertionError("A changed raw revision must invalidate approval")
        revise(revised, original[1] + 1)
        failure[0] = True
        try:
            correct_run(queue, 31, 1, expected_hash=digest, apply=True)
        except RuntimeError as exc:
            assert str(exc) == "Synthetic correction audit write failure"
        else:
            raise AssertionError("Synthetic audit failure did not execute")
        finally:
            failure[0] = False
        assert correct_run(queue, 31, 1)["approval_hash"] == digest
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(correct_run, queue, 31, 1, expected_hash=digest, apply=True) for _ in range(2)]
            concurrent = sorted(f.result()["status"] for f in futures)
        assert concurrent == ["already_committed", "committed"]
        assert normalize_hour(queue, 31, 1, confirm_timestamp_kwh=True, apply=True)["status"] == "already_committed"
        assert settle_run(queue, 31, 1, test_only=True, apply=True)["status"] == "already_committed"
        assert aggregate_dimensions(
            queue, 1, datetime.fromisoformat("2026-10-16T10:00:00+08:00"),
            datetime.fromisoformat("2026-10-17T10:00:00+08:00"), apply=True,
        )["status"] == "already_committed"
        with queue.transaction() as c:
            c.execute("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_events "
                      "WHERE event_type='business_correction_committed'")
            assert c.fetchone()[0] == 1
        assert signature(original_queue) == original
        print(json.dumps({"status": "passed", "fixture": "isolated_copy_of_nine_real_pilot_meters",
                          "source_original_unchanged": True, "affected_hours": 2,
                          "dimension_rows_verified": 12, "stale_approval_refused": True,
                          "independent_signed_business_deltas_checked": 18,
                          "audit_failure_rolled_back": True, "concurrent_results": concurrent,
                          "replayed_energy_settlement_dimensions": True}))
    finally:
        for clone in created:
            # Names are generated here, never supplied by input or a broad schema pattern.
            cursor.execute("DROP DATABASE `" + clone + "`")
        cursor.close()
        ddl.close()


if __name__ == "__main__":
    main()
