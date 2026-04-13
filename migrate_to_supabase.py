"""
Kjør én gang: flytter all data fra crm.db (SQLite) til Supabase PostgreSQL.
Bruk: python migrate_to_supabase.py
"""
import sqlite3, psycopg2, psycopg2.extras, os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

sqlite_conn = sqlite3.connect("crm.db")
sqlite_conn.row_factory = sqlite3.Row
pg_conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
pg_cur = pg_conn.cursor()

tables = ["contacts", "deals", "activities"]
total = 0

for table in tables:
    rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"{table}: 0 rader")
        continue

    cols = rows[0].keys()
    placeholders = ",".join(["%s"] * len(cols))
    col_str = ",".join(cols)

    inserted = 0
    for row in rows:
        vals = tuple(row[c] for c in cols)
        try:
            pg_cur.execute(
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                vals
            )
            inserted += 1
        except Exception as e:
            print(f"  Hopper over rad i {table}: {e}")

    print(f"{table}: {inserted}/{len(rows)} overført")
    total += inserted

pg_conn.commit()
pg_conn.close()
sqlite_conn.close()
print(f"\nFerdig! {total} rader totalt overført til Supabase.")
