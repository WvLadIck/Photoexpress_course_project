import math
import uuid
from pathlib import Path

from flask import Response, abort, flash
from flask_login import current_user
from werkzeug.utils import secure_filename

from config import ALLOWED_EXTENSIONS, UPLOAD_DIR, ROLE_ADMIN
from db import get_db, query_all, query_one
from users import display_name


def get_active_products():
    return query_all("SELECT * FROM products WHERE is_archived = 0 ORDER BY name")


def get_product(product_id: int):
    row = query_one("SELECT * FROM products WHERE id = ?", (product_id,))
    if row is None:
        abort(404)
    return row


def get_report(report_id: int):
    row = query_one(
        """
        SELECT r.*, u.login, u.first_name, u.last_name, u.middle_name
        FROM reports r
        JOIN users u ON u.id = r.photographer_id
        WHERE r.id = ?
        """,
        (report_id,),
    )
    if row is None:
        abort(404)
    return row


def ensure_report_access(report):
    if current_user.role_code == ROLE_ADMIN:
        return
    if int(report["photographer_id"]) != int(current_user.id):
        abort(403)


def get_photographer(user_id: int):
    row = query_one(
        """
        SELECT u.*, r.code AS role_code, r.name AS role_name
        FROM users u
        JOIN roles r ON r.id = u.role_id
        WHERE u.id = ? AND r.code = 'photographer'
        """,
        (user_id,),
    )
    if row is None:
        abort(404)
    return row


def parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_float(value):
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def parse_report_form(form, products):
    errors = []
    report_date = form.get("report_date", "").strip()
    match_title = form.get("match_title", "").strip()
    location = form.get("location", "").strip()
    note = form.get("note", "").strip()
    if not report_date:
        errors.append("Укажите дату отчета.")
    if not match_title:
        errors.append("Укажите название игры или мероприятия.")
    items = {}
    total_quantity = 0
    for product in products:
        raw_value = form.get(f"product_{product['id']}", "0")
        quantity = parse_int(raw_value)
        if quantity is None or quantity < 0:
            errors.append(f"Количество для «{product['name']}» должно быть неотрицательным целым числом.")
            continue
        items[product["id"]] = quantity
        total_quantity += quantity
    if total_quantity <= 0:
        errors.append("Нужно указать хотя бы одну проданную позицию.")
    return {"report_date": report_date, "match_title": match_title, "location": location, "note": note, "items": items}, errors


def parse_photographer_form(form, is_new: bool, current_user_id: int | None = None):
    errors = []
    login = form.get("login", "").strip()
    first_name = form.get("first_name", "").strip()
    last_name = form.get("last_name", "").strip()
    middle_name = form.get("middle_name", "").strip()
    password = form.get("password", "")
    is_active = 1 if form.get("is_active", "1") == "1" else 0
    if len(login) < 4:
        errors.append("Логин должен содержать не менее 4 символов.")
    if not first_name:
        errors.append("Укажите имя.")
    if not last_name:
        errors.append("Укажите фамилию.")
    if is_new and len(password) < 6:
        errors.append("Пароль должен содержать не менее 6 символов.")
    if password and len(password) < 6:
        errors.append("Новый пароль должен содержать не менее 6 символов.")
    existing = query_one("SELECT id FROM users WHERE login = ?", (login,))
    if existing and (current_user_id is None or existing["id"] != current_user_id):
        errors.append("Пользователь с таким логином уже существует.")
    return {
        "login": login,
        "first_name": first_name,
        "last_name": last_name,
        "middle_name": middle_name,
        "password": password,
        "is_active": is_active,
    }, errors


def compute_totals(items: dict[int, int]):
    total_items = sum(items.values())
    total_revenue = 0.0
    active_prices = {product["id"]: float(product["unit_price"]) for product in get_active_products()}
    for product_id, qty in items.items():
        total_revenue += qty * active_prices.get(product_id, 0)
    return total_items, total_revenue


def save_report_items(report_id: int, items: dict[int, int]):
    get_db().executemany(
        "INSERT INTO report_items (report_id, product_id, quantity) VALUES (?, ?, ?)",
        [(report_id, product_id, quantity) for product_id, quantity in items.items()],
    )


def replace_report_items(report_id: int, items: dict[int, int]):
    db = get_db()
    db.execute("DELETE FROM report_items WHERE report_id = ?", (report_id,))
    save_report_items(report_id, items)


def get_report_item_quantity_map(report_id: int) -> dict[int, int]:
    rows = query_all("SELECT product_id, quantity FROM report_items WHERE report_id = ?", (report_id,))
    return {row["product_id"]: row["quantity"] for row in rows}


def get_report_items(report_id: int):
    return query_all(
        """
        SELECT ri.quantity, p.name AS product_name, p.unit_price, ri.quantity * p.unit_price AS revenue
        FROM report_items ri
        JOIN products p ON p.id = ri.product_id
        WHERE ri.report_id = ?
        ORDER BY p.name
        """,
        (report_id,),
    )


def get_report_attachments(report_id: int):
    return query_all("SELECT * FROM report_attachments WHERE report_id = ? ORDER BY created_at DESC", (report_id,))


def get_report_comments(report_id: int):
    return query_all(
        """
        SELECT rc.*, u.last_name, u.first_name, u.middle_name
        FROM report_comments rc
        JOIN users u ON u.id = rc.admin_id
        WHERE rc.report_id = ?
        ORDER BY rc.created_at DESC
        """,
        (report_id,),
    )


def calculate_item_deltas(old_items: dict[int, int], new_items: dict[int, int]) -> dict[int, int]:
    deltas = {}
    for product_id in set(old_items) | set(new_items):
        deltas[product_id] = new_items.get(product_id, 0) - old_items.get(product_id, 0)
    return deltas


def ensure_stock_available(deltas: dict[int, int]):
    errors = []
    for product_id, delta in deltas.items():
        if delta <= 0:
            continue
        product = get_product(product_id)
        if product["current_stock"] < delta:
            errors.append(
                f"Недостаточно остатка по материалу «{product['name']}». Доступно: {product['current_stock']}, требуется: {delta}."
            )
    return errors


def apply_stock_delta(deltas: dict[int, int]):
    db = get_db()
    for product_id, delta in deltas.items():
        if delta == 0:
            continue
        db.execute("UPDATE products SET current_stock = current_stock - ? WHERE id = ?", (delta, product_id))
        db.execute(
            "INSERT INTO stock_movements (product_id, movement_type, quantity, note, created_by, created_at) VALUES (?, 'sale_adjustment', ?, ?, ?, CURRENT_TIMESTAMP)",
            (product_id, -delta, "Автоматическое изменение остатка по отчету", current_user.id if current_user.is_authenticated else None),
        )


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_files(report_id: int, user_id: int, files):
    db = get_db()
    for file in files:
        if not file or not file.filename:
            continue
        if not allowed_file(file.filename):
            flash(f"Файл {file.filename} пропущен: недопустимый формат.", "warning")
            continue
        original_filename = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{original_filename}"
        file.save(UPLOAD_DIR / unique_name)
        db.execute(
            "INSERT INTO report_attachments (report_id, original_filename, stored_filename, created_by, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (report_id, original_filename, unique_name, user_id),
        )


def delete_file_if_exists(filename: str):
    path = UPLOAD_DIR / filename
    if path.exists():
        path.unlink(missing_ok=True)


def get_summary_for_photographer(user_id: int):
    product_rows = query_all(
        """
        SELECT p.name, SUM(ri.quantity) AS qty, SUM(ri.quantity * p.unit_price) AS revenue
        FROM reports r
        JOIN report_items ri ON ri.report_id = r.id
        JOIN products p ON p.id = ri.product_id
        WHERE r.photographer_id = ?
        GROUP BY p.id, p.name
        ORDER BY p.name
        """,
        (user_id,),
    )
    totals_row = query_one(
        "SELECT COUNT(*) AS reports_count, COALESCE(SUM(total_revenue), 0) AS total_revenue, COALESCE(SUM(total_items), 0) AS total_items FROM reports WHERE photographer_id = ?",
        (user_id,),
    )
    return build_summary(product_rows, totals_row)


def get_global_summary():
    product_rows = query_all(
        """
        SELECT p.name, SUM(ri.quantity) AS qty, SUM(ri.quantity * p.unit_price) AS revenue
        FROM reports r
        JOIN report_items ri ON ri.report_id = r.id
        JOIN products p ON p.id = ri.product_id
        GROUP BY p.id, p.name
        ORDER BY p.name
        """
    )
    totals_row = query_one(
        "SELECT COUNT(*) AS reports_count, COALESCE(SUM(total_revenue), 0) AS total_revenue, COALESCE(SUM(total_items), 0) AS total_items FROM reports"
    )
    return build_summary(product_rows, totals_row)


def build_summary(product_rows, totals_row):
    quantity_by_product = {row["name"]: int(row["qty"] or 0) for row in product_rows}
    revenue_by_product = {row["name"]: float(row["revenue"] or 0) for row in product_rows}
    return {
        "reports_count": int(totals_row["reports_count"] or 0),
        "total_revenue": float(totals_row["total_revenue"] or 0),
        "total_items": int(totals_row["total_items"] or 0),
        "quantity_by_product": quantity_by_product,
        "revenue_by_product": revenue_by_product,
    }


def get_photographer_ranking():
    rows = query_all(
        """
        SELECT u.last_name, u.first_name, u.middle_name,
               COALESCE(SUM(r.total_items), 0) AS items_sold,
               COALESCE(SUM(r.total_revenue), 0) AS revenue
        FROM users u
        JOIN roles rl ON rl.id = u.role_id AND rl.code = 'photographer'
        LEFT JOIN reports r ON r.photographer_id = u.id
        GROUP BY u.id
        ORDER BY revenue DESC, items_sold DESC, u.last_name ASC
        """
    )
    ranking = []
    for row in rows:
        ranking.append({"full_name": display_name(row), "items_sold": int(row["items_sold"] or 0), "revenue": float(row["revenue"] or 0)})
    return ranking


def csv_response(content: str, filename: str):
    return Response(content, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={filename}"})


def build_pie_svg(values: dict[str, float]):
    filtered = [(label, float(value)) for label, value in values.items() if value and value > 0]
    if not filtered:
        return ""
    palette = ["#8093F1", "#9B5DE5", "#4CC9F0", "#4CAF50", "#F4A261", "#E76F51"]
    total = sum(value for _, value in filtered)
    center = 120
    radius = 92
    start_angle = -math.pi / 2
    paths = []
    for index, (_, value) in enumerate(filtered):
        angle = 2 * math.pi * (value / total)
        end_angle = start_angle + angle
        x1 = center + radius * math.cos(start_angle)
        y1 = center + radius * math.sin(start_angle)
        x2 = center + radius * math.cos(end_angle)
        y2 = center + radius * math.sin(end_angle)
        large_arc = 1 if angle > math.pi else 0
        color = palette[index % len(palette)]
        paths.append(f'<path d="M {center} {center} L {x1:.2f} {y1:.2f} A {radius} {radius} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z" fill="{color}"></path>')
        start_angle = end_angle
    return f'<svg viewBox="0 0 240 240" class="pie-chart" aria-hidden="true">{"".join(paths)}</svg>'
