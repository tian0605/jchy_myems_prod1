"""Initialize synthetic fixtures ONLY in the dedicated Compose acceptance database."""

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import mysql.connector
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def main():
    if os.environ.get("MYEMS_SYSTEM_DB_HOST") != "db":
        raise RuntimeError("This bootstrap is restricted to the isolated Compose database")
    state = Path("/acceptance/state")
    state.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mock")])
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(minutes=5))
        .not_valid_after(datetime.utcnow() + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("mock")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (state / "mock.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    (state / "mock.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (state / "scenario.json").write_text('{"missing":true}', encoding="utf-8")
    connection = mysql.connector.connect(
        host="db", user="root", password=os.environ["MYEMS_SYSTEM_DB_PASSWORD"],
        database="myems_system_db",
    )
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM tbl_data_sources")
        if cursor.fetchone()[0]:
            raise RuntimeError("Bootstrap requires a pristine synthetic database; existing data will not be overwritten")
        cursor.execute(
            "UPDATE tbl_gateways SET name='LOCAL SYNTHETIC gateway',token=%s WHERE id=1", (str(uuid4()),),
        )
        cursor.execute(
            "INSERT INTO tbl_data_sources (id,name,uuid,gateway_id,protocol,connection,description) "
            "VALUES (1,'LOCAL SYNTHETIC Jishiyun',%s,1,'JISHIYUN','{}','Isolated acceptance only')", (str(uuid4()),),
        )
        cursor.execute("INSERT INTO tbl_cost_centers (id,name,uuid) VALUES (1,'LOCAL cost center',%s)", (str(uuid4()),))
        cursor.execute(
            "INSERT INTO tbl_energy_categories (id,name,uuid,unit_of_measure,kgce,kgco2e) "
            "VALUES (1,'LOCAL electricity',%s,'kWh',0,0)", (str(uuid4()),),
        )
        for task_id, kind in ((1, "meter_initialize"), (2, "energy_collect"), (3, "energy_backfill")):
            cursor.execute(
                "INSERT INTO tbl_jishiyun_tasks (id,data_source_id,name,task_type,is_enabled,"
                "provider_utc_offset_minutes,parameters_json,created_by_user_id,updated_by_user_id) "
                "VALUES (%s,1,%s,%s,1,480,'{\"encoding\":\"json\"}',1,1)",
                (task_id, "LOCAL " + kind, kind),
            )
        salt = uuid4().hex
        admin_password = os.environ["LOCAL_ADMIN_PASSWORD"]
        digest = hashlib.sha512((salt + admin_password).encode()).hexdigest()
        cursor.execute(
            "UPDATE myems_user_db.tbl_users SET salt=%s,password=%s WHERE id=1", (salt, digest),
        )
        connection.commit()
        (state / "login.json").write_text(
            json.dumps({"username": "administrator", "password": admin_password}), encoding="utf-8",
        )
    finally:
        cursor.close()
        connection.close()
    print("Synthetic source/tasks and private mock TLS certificate initialized; login saved in ignored state/login.json")


if __name__ == "__main__":
    main()
