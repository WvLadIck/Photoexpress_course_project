import sqlite3
from functools import wraps

from flask import abort, redirect, url_for
from flask_login import UserMixin, current_user

from config import ROLE_ADMIN, ROLE_PHOTOGRAPHER
from db import query_one
from extensions import login_manager


class AppUser(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = str(row["id"])
        self.login = row["login"]
        self.password_hash = row["password_hash"]
        self.first_name = row["first_name"]
        self.last_name = row["last_name"]
        self.middle_name = row["middle_name"]
        self.role_code = row["role_code"]
        self.role_name = row["role_name"]
        self._is_active = bool(row["is_active"])

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def full_name(self) -> str:
        return display_name(self.__dict__)


@login_manager.user_loader
def load_user(user_id: str):
    row = query_one(
        """
        SELECT u.*, r.code AS role_code, r.name AS role_name
        FROM users u
        JOIN roles r ON r.id = u.role_id
        WHERE u.id = ? AND u.is_active = 1
        """,
        (user_id,),
    )
    if row is None:
        return None
    return AppUser(row)


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role_code not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def redirect_for_role(role_code: str):
    if role_code == ROLE_ADMIN:
        return redirect(url_for("admin_stats"))
    return redirect(url_for("photographer_stats"))


def is_safe_next_url(target: str) -> bool:
    return target.startswith("/") and not target.startswith("//")


def display_name(row) -> str:
    if isinstance(row, sqlite3.Row):
        last_name = row["last_name"]
        first_name = row["first_name"]
        middle_name = row["middle_name"]
        login = row["login"] if "login" in row.keys() else ""
    else:
        last_name = row.get("last_name", "")
        first_name = row.get("first_name", "")
        middle_name = row.get("middle_name", "")
        login = row.get("login", "")
    full = " ".join(part for part in [last_name or "", first_name or "", middle_name or ""] if part).strip()
    return full or login or "Пользователь"


def status_label(status: str) -> str:
    return {"submitted": "На проверке", "reviewed": "Проверен", "draft": "Черновик"}.get(status, status)


def get_role_id(code: str) -> int:
    row = query_one("SELECT id FROM roles WHERE code = ?", (code,))
    if row is None:
        raise RuntimeError(f"Role {code} not found")
    return row["id"]
