"""Execute the actual collector SELECT without starting its unbounded daemon."""

import ast
from pathlib import Path
import sqlite3
import unittest


class JishiyunWriterOwnershipTests(unittest.TestCase):
    def test_six_dimension_loops_exclude_explicit_pilot_owned_objects(self):
        for dimension in ("space", "equipment"):
            table = "tbl_spaces" if dimension == "space" else "tbl_equipments"
            for kind in ("energy", "billing", "carbon"):
                module = dimension + "_" + kind + "_input_category.py"
                with self.subTest(module=module), sqlite3.connect(":memory:") as connection:
                    connection.execute("CREATE TABLE " + table
                                       + "(id INTEGER,name TEXT,description TEXT,cost_center_id INTEGER DEFAULT 1)")
                    connection.executemany(
                        "INSERT INTO " + table + " (id,name,description) VALUES (?,?,?)",
                        [(1, "ordinary", None), (2, "pilot", "JISHIYUN_PILOT_SOURCE:4"),
                         (3, "ordinary", "other")],
                    )
                    tree = ast.parse((Path(__file__).parent / module).read_text("utf-8"))
                    selections = [
                        node.args[0].value for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "execute" and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                        and "JISHIYUN_PILOT_SOURCE" in node.args[0].value
                    ]
                    self.assertEqual(len(selections), 1)
                    self.assertEqual([row[0] for row in connection.execute(selections[0])], [1, 3])

    def test_billing_and_carbon_exclude_dedicated_staged_writers_only(self):
        for module in ("meter_billing.py", "meter_carbon.py"):
            with self.subTest(module=module), sqlite3.connect(":memory:") as connection:
                connection.executescript("""
                    CREATE TABLE tbl_meters (id INTEGER,name TEXT,energy_category_id INTEGER,cost_center_id INTEGER);
                    CREATE TABLE tbl_points (id INTEGER,object_type TEXT);
                    CREATE TABLE tbl_meters_points (meter_id INTEGER,point_id INTEGER);
                    INSERT INTO tbl_meters VALUES (1,'legacy',1,1),(2,'unmapped',1,1),
                        (3,'pilot',1,1),(4,'mixed',1,1);
                    INSERT INTO tbl_points VALUES (1,'ENERGY_VALUE'),(2,'JISHIYUN_REGISTER');
                    INSERT INTO tbl_meters_points VALUES (1,1),(3,2),(4,1),(4,2);
                """)
                tree = ast.parse((Path(__file__).parent / module).read_text("utf-8"))
                selections = [
                    node.args[0].value for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and "FROM tbl_meters m " in node.args[0].value
                ]
                self.assertEqual(len(selections), 1)
                self.assertEqual([row[0] for row in connection.execute(selections[0])], [1, 2])


if __name__ == "__main__":
    unittest.main()
