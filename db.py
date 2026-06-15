import sqlite3

from flask import g
from werkzeug.security import generate_password_hash

from config import BASE_DIR, DATABASE, PRODUCT_SEED, ROLE_ADMIN, ROLE_PHOTOGRAPHER


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_one(query: str, params=()):
    return get_db().execute(query, params).fetchone()


def query_all(query: str, params=()):
    return get_db().execute(query, params).fetchall()


def init_db(force: bool = False):
    if DATABASE.exists() and not force:
        return
    schema = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if force:
        db.executescript(
            """
            DROP TABLE IF EXISTS stock_movements;
            DROP TABLE IF EXISTS report_comments;
            DROP TABLE IF EXISTS report_attachments;
            DROP TABLE IF EXISTS report_items;
            DROP TABLE IF EXISTS reports;
            DROP TABLE IF EXISTS products;
            DROP TABLE IF EXISTS users;
            DROP TABLE IF EXISTS roles;
            """
        )
    db.executescript(schema)
    seed_roles(db)
    seed_products(db)
    seed_users(db)
    db.commit()
    db.close()


def seed_roles(db: sqlite3.Connection):
    if db.execute("SELECT COUNT(*) AS cnt FROM roles").fetchone()["cnt"]:
        return
    db.executemany(
        "INSERT INTO roles (code, name) VALUES (?, ?)",
        [(ROLE_ADMIN, "Администратор"), (ROLE_PHOTOGRAPHER, "Фотограф")],
    )


def seed_products(db: sqlite3.Connection):
    if db.execute("SELECT COUNT(*) AS cnt FROM products").fetchone()["cnt"]:
        return
    db.executemany(
        "INSERT INTO products (code, name, unit_price, current_stock, is_archived) VALUES (?, ?, ?, ?, 0)",
        [(item["code"], item["name"], item["unit_price"], item["current_stock"]) for item in PRODUCT_SEED],
    )


def seed_users(db: sqlite3.Connection):
    if db.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]:
        return
    role_admin = db.execute("SELECT id FROM roles WHERE code = ?", (ROLE_ADMIN,)).fetchone()["id"]
    role_photographer = db.execute("SELECT id FROM roles WHERE code = ?", (ROLE_PHOTOGRAPHER,)).fetchone()["id"]
    db.executemany(
        """
        INSERT INTO users (login, password_hash, first_name, last_name, middle_name, role_id, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        """,
        [
            ("admin", generate_password_hash("admin123"), "Главный", "Администратор", "", role_admin),
            ("photo1", generate_password_hash("photo123"), "Иван", "Иванов", "Иванович", role_photographer),
            ("photo2", generate_password_hash("photo123"), "Аслан", "Казаков", "Асланович", role_photographer),
        ],
    )
