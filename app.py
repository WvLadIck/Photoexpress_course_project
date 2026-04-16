import csv
import io
import math
import sqlite3
import uuid
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = BASE_DIR / "uploads"
DATABASE = INSTANCE_DIR / "photoexpress.db"
ALLOWED_EXTENSIONS = {"pdf", "csv", "xlsx", "xls", "jpg", "jpeg", "png"}
ROLE_ADMIN = "admin"
ROLE_PHOTOGRAPHER = "photographer"
PRODUCT_SEED = [
    {"code": "photo_a5", "name": "Фото A5", "unit_price": 250.0, "current_stock": 500},
    {"code": "photo_a4", "name": "Фото A4", "unit_price": 450.0, "current_stock": 400},
    {"code": "frame_a5", "name": "Рамка A5", "unit_price": 350.0, "current_stock": 300},
    {"code": "frame_a4", "name": "Рамка A4", "unit_price": 600.0, "current_stock": 250},
]
NAV_ITEMS = {
    ROLE_ADMIN: [("admin_stats", "Статистика"), ("admin_photographers", "Фотографы"), ("admin_reports", "Отчеты")],
    ROLE_PHOTOGRAPHER: [("photographer_stats", "Статистика"), ("my_reports", "Мои отчеты")],
}

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Сначала выполните вход."
login_manager.login_message_category = "warning"


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "change-me-before-production"
    app.config["DATABASE"] = str(DATABASE)
    app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)

    INSTANCE_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)

    login_manager.init_app(app)
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db(force=True)
        print("База данных инициализирована.")

    @app.route("/")
    def root():
        if not current_user.is_authenticated:
            return redirect(url_for("login"))
        return redirect_for_role(current_user.role_code)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect_for_role(current_user.role_code)

        if request.method == "POST":
            login_value = request.form.get("login", "").strip()
            password = request.form.get("password", "")
            next_url = request.form.get("next", "").strip()
            row = query_one(
                """
                SELECT u.*, r.code AS role_code, r.name AS role_name
                FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.login = ? AND u.is_active = 1
                """,
                (login_value,),
            )
            if row and check_password_hash(row["password_hash"], password):
                login_user(AppUser(row))
                flash("Вход выполнен успешно.", "success")
                if next_url and is_safe_next_url(next_url):
                    return redirect(next_url)
                return redirect_for_role(row["role_code"])
            flash("Неверный логин или пароль.", "danger")

        return render_template("auth/login.html", next=request.args.get("next", ""))

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Вы вышли из системы.", "info")
        return redirect(url_for("login"))

    @app.route("/photographer/stats")
    @login_required
    @role_required(ROLE_PHOTOGRAPHER)
    def photographer_stats():
        stats = get_summary_for_photographer(int(current_user.id))
        recent_reports = query_all(
            """
            SELECT r.*, COALESCE((SELECT COUNT(*) FROM report_comments rc WHERE rc.report_id = r.id), 0) AS comments_count
            FROM reports r
            WHERE r.photographer_id = ?
            ORDER BY r.report_date DESC, r.created_at DESC
            LIMIT 5
            """,
            (current_user.id,),
        )
        return render_template(
            "photographer/stats.html",
            stats=stats,
            chart_svg=build_pie_svg(stats["revenue_by_product"]),
            recent_reports=recent_reports,
        )

    @app.route("/reports")
    @login_required
    @role_required(ROLE_PHOTOGRAPHER)
    def my_reports():
        reports = query_all(
            """
            SELECT r.*, COALESCE((SELECT COUNT(*) FROM report_comments rc WHERE rc.report_id = r.id), 0) AS comments_count
            FROM reports r
            WHERE r.photographer_id = ?
            ORDER BY r.report_date DESC, r.created_at DESC
            """,
            (current_user.id,),
        )
        return render_template("reports/list.html", reports=reports)

    @app.route("/reports/new", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_PHOTOGRAPHER)
    def create_report():
        products = get_active_products()
        if request.method == "POST":
            payload, errors = parse_report_form(request.form, products)
            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template("reports/form.html", products=products, mode="create", values=request.form)
            stock_errors = ensure_stock_available(payload["items"])
            if stock_errors:
                for error in stock_errors:
                    flash(error, "danger")
                return render_template("reports/form.html", products=products, mode="create", values=request.form)

            total_items, total_revenue = compute_totals(payload["items"])
            db = get_db()
            cursor = db.execute(
                """
                INSERT INTO reports (
                    photographer_id, report_date, match_title, location, note, status,
                    total_items, total_revenue, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    current_user.id,
                    payload["report_date"],
                    payload["match_title"],
                    payload["location"],
                    payload["note"],
                    total_items,
                    total_revenue,
                ),
            )
            report_id = cursor.lastrowid
            save_report_items(report_id, payload["items"])
            apply_stock_delta(payload["items"])
            save_uploaded_files(report_id, current_user.id, request.files.getlist("files"))
            db.commit()
            flash("Отчет создан.", "success")
            return redirect(url_for("report_detail", report_id=report_id))

        return render_template("reports/form.html", products=products, mode="create", values={})

    @app.route("/reports/<int:report_id>")
    @login_required
    def report_detail(report_id: int):
        report = get_report(report_id)
        ensure_report_access(report)
        items = get_report_items(report_id)
        attachments = get_report_attachments(report_id)
        comments = get_report_comments(report_id)
        chart_svg = build_pie_svg({row["product_name"]: row["revenue"] for row in items})
        return render_template(
            "reports/detail.html",
            report=report,
            items=items,
            attachments=attachments,
            comments=comments,
            chart_svg=chart_svg,
        )

    @app.route("/reports/<int:report_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_PHOTOGRAPHER)
    def edit_report(report_id: int):
        report = get_report(report_id)
        if int(report["photographer_id"]) != int(current_user.id):
            abort(403)
        if report["status"] == "reviewed":
            flash("Проверенный отчет редактировать нельзя.", "warning")
            return redirect(url_for("report_detail", report_id=report_id))

        products = get_active_products()
        existing_map = get_report_item_quantity_map(report_id)

        if request.method == "POST":
            payload, errors = parse_report_form(request.form, products)
            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template("reports/form.html", products=products, mode="edit", report=report, values=request.form)
            deltas = calculate_item_deltas(existing_map, payload["items"])
            stock_errors = ensure_stock_available(deltas)
            if stock_errors:
                for error in stock_errors:
                    flash(error, "danger")
                return render_template("reports/form.html", products=products, mode="edit", report=report, values=request.form)

            total_items, total_revenue = compute_totals(payload["items"])
            db = get_db()
            db.execute(
                """
                UPDATE reports
                SET report_date = ?, match_title = ?, location = ?, note = ?,
                    total_items = ?, total_revenue = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload["report_date"],
                    payload["match_title"],
                    payload["location"],
                    payload["note"],
                    total_items,
                    total_revenue,
                    report_id,
                ),
            )
            replace_report_items(report_id, payload["items"])
            apply_stock_delta(deltas)
            save_uploaded_files(report_id, current_user.id, request.files.getlist("files"))
            db.commit()
            flash("Отчет обновлен.", "success")
            return redirect(url_for("report_detail", report_id=report_id))

        values = {
            "report_date": report["report_date"],
            "match_title": report["match_title"],
            "location": report["location"],
            "note": report["note"],
        }
        for product in products:
            values[f"product_{product['id']}"] = existing_map.get(product["id"], 0)
        return render_template("reports/form.html", products=products, mode="edit", report=report, values=values)

    @app.route("/reports/<int:report_id>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_PHOTOGRAPHER)
    def delete_report(report_id: int):
        report = get_report(report_id)
        if int(report["photographer_id"]) != int(current_user.id):
            abort(403)
        if report["status"] == "reviewed":
            flash("Нельзя удалить проверенный отчет.", "warning")
            return redirect(url_for("report_detail", report_id=report_id))
        items = get_report_item_quantity_map(report_id)
        db = get_db()
        for attachment in get_report_attachments(report_id):
            delete_file_if_exists(attachment["stored_filename"])
        db.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        apply_stock_delta({pid: -qty for pid, qty in items.items()})
        db.commit()
        flash("Отчет удален.", "info")
        return redirect(url_for("my_reports"))

    @app.route("/attachments/<int:attachment_id>/download")
    @login_required
    def download_attachment(attachment_id: int):
        attachment = query_one(
            """
            SELECT a.*, r.photographer_id
            FROM report_attachments a
            JOIN reports r ON r.id = a.report_id
            WHERE a.id = ?
            """,
            (attachment_id,),
        )
        if attachment is None:
            abort(404)
        if current_user.role_code != ROLE_ADMIN and int(attachment["photographer_id"]) != int(current_user.id):
            abort(403)
        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            attachment["stored_filename"],
            as_attachment=True,
            download_name=attachment["original_filename"],
        )

    @app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
    @login_required
    def delete_attachment(attachment_id: int):
        attachment = query_one(
            """
            SELECT a.*, r.photographer_id, r.status
            FROM report_attachments a
            JOIN reports r ON r.id = a.report_id
            WHERE a.id = ?
            """,
            (attachment_id,),
        )
        if attachment is None:
            abort(404)
        if current_user.role_code != ROLE_ADMIN and int(attachment["photographer_id"]) != int(current_user.id):
            abort(403)
        if current_user.role_code != ROLE_ADMIN and attachment["status"] == "reviewed":
            flash("Нельзя менять вложения у проверенного отчета.", "warning")
            return redirect(url_for("report_detail", report_id=attachment["report_id"]))
        get_db().execute("DELETE FROM report_attachments WHERE id = ?", (attachment_id,))
        get_db().commit()
        delete_file_if_exists(attachment["stored_filename"])
        flash("Файл удален.", "info")
        return redirect(url_for("report_detail", report_id=attachment["report_id"]))

    @app.route("/admin/stats")
    @login_required
    @role_required(ROLE_ADMIN)
    def admin_stats():
        stats = get_global_summary()
        ranking = get_photographer_ranking()
        low_stock = query_all("SELECT * FROM products WHERE is_archived = 0 ORDER BY current_stock ASC, name ASC LIMIT 5")
        return render_template(
            "admin/stats.html",
            stats=stats,
            chart_svg=build_pie_svg(stats["revenue_by_product"]),
            ranking=ranking,
            low_stock=low_stock,
        )

    @app.route("/admin/photographers")
    @login_required
    @role_required(ROLE_ADMIN)
    def admin_photographers():
        photographers = query_all(
            """
            SELECT u.*, COALESCE((SELECT COUNT(*) FROM reports rp WHERE rp.photographer_id = u.id), 0) AS reports_count,
                   COALESCE((SELECT SUM(total_revenue) FROM reports rp WHERE rp.photographer_id = u.id), 0) AS total_revenue
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE r.code = 'photographer'
            ORDER BY u.last_name, u.first_name, u.middle_name
            """
        )
        return render_template("admin/photographers_list.html", photographers=photographers)

    @app.route("/admin/photographers/new", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def create_photographer():
        if request.method == "POST":
            data, errors = parse_photographer_form(request.form, is_new=True)
            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template("admin/photographer_form.html", mode="create", values=request.form)
            db = get_db()
            db.execute(
                """
                INSERT INTO users (login, password_hash, first_name, last_name, middle_name, role_id, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                """,
                (
                    data["login"],
                    generate_password_hash(data["password"]),
                    data["first_name"],
                    data["last_name"],
                    data["middle_name"],
                    get_role_id(ROLE_PHOTOGRAPHER),
                ),
            )
            db.commit()
            flash("Аккаунт фотографа создан.", "success")
            return redirect(url_for("admin_photographers"))
        return render_template("admin/photographer_form.html", mode="create", values={})

    @app.route("/admin/photographers/<int:user_id>")
    @login_required
    @role_required(ROLE_ADMIN)
    def photographer_detail(user_id: int):
        photographer = get_photographer(user_id)
        stats = get_summary_for_photographer(user_id)
        recent_reports = query_all(
            "SELECT * FROM reports WHERE photographer_id = ? ORDER BY report_date DESC, created_at DESC LIMIT 10",
            (user_id,),
        )
        return render_template(
            "admin/photographer_detail.html",
            photographer=photographer,
            stats=stats,
            chart_svg=build_pie_svg(stats["revenue_by_product"]),
            recent_reports=recent_reports,
        )

    @app.route("/admin/photographers/<int:user_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def edit_photographer(user_id: int):
        photographer = get_photographer(user_id)
        if request.method == "POST":
            data, errors = parse_photographer_form(request.form, is_new=False, current_user_id=user_id)
            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template("admin/photographer_form.html", mode="edit", photographer=photographer, values=request.form)
            db = get_db()
            db.execute(
                "UPDATE users SET login = ?, first_name = ?, last_name = ?, middle_name = ?, is_active = ? WHERE id = ?",
                (data["login"], data["first_name"], data["last_name"], data["middle_name"], data["is_active"], user_id),
            )
            if data["password"]:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(data["password"]), user_id))
            db.commit()
            flash("Данные фотографа обновлены.", "success")
            return redirect(url_for("photographer_detail", user_id=user_id))
        values = {
            "login": photographer["login"],
            "first_name": photographer["first_name"],
            "last_name": photographer["last_name"],
            "middle_name": photographer["middle_name"],
            "is_active": str(photographer["is_active"]),
        }
        return render_template("admin/photographer_form.html", mode="edit", photographer=photographer, values=values)

    @app.route("/admin/photographers/<int:user_id>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def delete_photographer(user_id: int):
        photographer = get_photographer(user_id)
        report_count = query_one("SELECT COUNT(*) AS cnt FROM reports WHERE photographer_id = ?", (user_id,))["cnt"]
        if report_count:
            flash("Нельзя удалить фотографа, у которого уже есть отчеты.", "warning")
            return redirect(url_for("photographer_detail", user_id=user_id))
        get_db().execute("DELETE FROM users WHERE id = ?", (user_id,))
        get_db().commit()
        flash(f"Фотограф {display_name(photographer)} удален.", "info")
        return redirect(url_for("admin_photographers"))

    @app.route("/admin/reports")
    @login_required
    @role_required(ROLE_ADMIN)
    def admin_reports():
        reports = query_all(
            """
            SELECT r.*, u.last_name, u.first_name, u.middle_name,
                   COALESCE((SELECT COUNT(*) FROM report_comments rc WHERE rc.report_id = r.id), 0) AS comments_count
            FROM reports r
            JOIN users u ON u.id = r.photographer_id
            ORDER BY r.report_date DESC, r.created_at DESC
            """
        )
        return render_template("admin/reports_list.html", reports=reports)

    @app.route("/admin/reports/<int:report_id>", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def admin_report_detail(report_id: int):
        report = get_report(report_id)
        items = get_report_items(report_id)
        attachments = get_report_attachments(report_id)
        comments = get_report_comments(report_id)
        if request.method == "POST":
            comment_text = request.form.get("comment_text", "").strip()
            if not comment_text:
                flash("Комментарий не может быть пустым.", "danger")
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO report_comments (report_id, admin_id, comment_text, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                    (report_id, current_user.id, comment_text),
                )
                db.execute("UPDATE reports SET status = 'reviewed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (report_id,))
                db.commit()
                flash("Комментарий сохранен.", "success")
                return redirect(url_for("admin_report_detail", report_id=report_id))
        chart_svg = build_pie_svg({row["product_name"]: row["revenue"] for row in items})
        return render_template(
            "admin/report_detail.html",
            report=report,
            items=items,
            attachments=attachments,
            comments=comments,
            chart_svg=chart_svg,
        )

    @app.route("/admin/consumables", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def admin_consumables():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "create":
                name = request.form.get("name", "").strip()
                code = request.form.get("code", "").strip().lower()
                unit_price = parse_float(request.form.get("unit_price", "0"))
                current_stock = parse_int(request.form.get("current_stock", "0"))
                errors = []
                if not name:
                    errors.append("Укажите название материала.")
                if not code:
                    errors.append("Укажите код материала латиницей.")
                if unit_price is None or unit_price < 0:
                    errors.append("Цена должна быть неотрицательной.")
                if current_stock is None or current_stock < 0:
                    errors.append("Остаток должен быть неотрицательным.")
                if query_one("SELECT 1 FROM products WHERE code = ?", (code,)):
                    errors.append("Материал с таким кодом уже существует.")
                if errors:
                    for error in errors:
                        flash(error, "danger")
                else:
                    db = get_db()
                    db.execute(
                        "INSERT INTO products (code, name, unit_price, current_stock, is_archived) VALUES (?, ?, ?, ?, 0)",
                        (code, name, unit_price, current_stock),
                    )
                    db.commit()
                    flash("Материал добавлен.", "success")
                    return redirect(url_for("admin_consumables"))
            elif action == "restock":
                product_id = parse_int(request.form.get("product_id", "0"))
                quantity = parse_int(request.form.get("quantity", "0"))
                note = request.form.get("note", "").strip()
                if not product_id or quantity is None or quantity <= 0:
                    flash("Количество пополнения должно быть больше нуля.", "danger")
                else:
                    db = get_db()
                    db.execute("UPDATE products SET current_stock = current_stock + ? WHERE id = ?", (quantity, product_id))
                    db.execute(
                        "INSERT INTO stock_movements (product_id, movement_type, quantity, note, created_by, created_at) VALUES (?, 'income', ?, ?, ?, CURRENT_TIMESTAMP)",
                        (product_id, quantity, note or "Пополнение со страницы администратора", current_user.id),
                    )
                    db.commit()
                    flash("Остаток обновлен.", "success")
                    return redirect(url_for("admin_consumables"))

        products = query_all("SELECT * FROM products ORDER BY is_archived, name")
        movements = query_all(
            """
            SELECT sm.*, p.name AS product_name, u.last_name, u.first_name, u.middle_name
            FROM stock_movements sm
            JOIN products p ON p.id = sm.product_id
            LEFT JOIN users u ON u.id = sm.created_by
            ORDER BY sm.created_at DESC
            LIMIT 15
            """
        )
        return render_template("admin/consumables.html", products=products, movements=movements)

    @app.route("/admin/consumables/<int:product_id>/edit", methods=["POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def edit_consumable(product_id: int):
        product = get_product(product_id)
        name = request.form.get("name", "").strip()
        unit_price = parse_float(request.form.get("unit_price", "0"))
        is_archived = 1 if request.form.get("is_archived") == "1" else 0
        if not name:
            flash("Название не может быть пустым.", "danger")
        elif unit_price is None or unit_price < 0:
            flash("Цена должна быть неотрицательной.", "danger")
        else:
            get_db().execute(
                "UPDATE products SET name = ?, unit_price = ?, is_archived = ? WHERE id = ?",
                (name, unit_price, is_archived, product_id),
            )
            get_db().commit()
            flash(f"Материал «{product['name']}» обновлен.", "success")
        return redirect(url_for("admin_consumables"))

    @app.route("/admin/consumables/<int:product_id>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_ADMIN)
    def delete_consumable(product_id: int):
        product = get_product(product_id)
        usage = query_one("SELECT COUNT(*) AS cnt FROM report_items WHERE product_id = ?", (product_id,))["cnt"]
        if usage:
            flash("Нельзя удалить материал, который уже используется в отчетах.", "warning")
        else:
            get_db().execute("DELETE FROM products WHERE id = ?", (product_id,))
            get_db().commit()
            flash(f"Материал «{product['name']}» удален.", "info")
        return redirect(url_for("admin_consumables"))

    @app.route("/exports/reports.csv")
    @login_required
    def export_reports_csv():
        if current_user.role_code == ROLE_ADMIN:
            rows = query_all(
                """
                SELECT r.id, r.report_date, r.match_title, r.location, r.status, r.total_items, r.total_revenue,
                       u.last_name, u.first_name, u.middle_name
                FROM reports r
                JOIN users u ON u.id = r.photographer_id
                ORDER BY r.report_date DESC, r.created_at DESC
                """
            )
            filename = "all_reports.csv"
        else:
            rows = query_all(
                """
                SELECT r.id, r.report_date, r.match_title, r.location, r.status, r.total_items, r.total_revenue,
                       u.last_name, u.first_name, u.middle_name
                FROM reports r
                JOIN users u ON u.id = r.photographer_id
                WHERE r.photographer_id = ?
                ORDER BY r.report_date DESC, r.created_at DESC
                """,
                (current_user.id,),
            )
            filename = "my_reports.csv"

        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["ID", "Дата", "Игра", "Локация", "Статус", "Количество", "Выручка", "Фотограф"])
        for row in rows:
            writer.writerow([
                row["id"],
                row["report_date"],
                row["match_title"],
                row["location"],
                row["status"],
                row["total_items"],
                f"{row['total_revenue']:.2f}",
                display_name(row),
            ])
        return csv_response(output.getvalue(), filename)

    @app.route("/exports/stats.csv")
    @login_required
    @role_required(ROLE_ADMIN)
    def export_stats_csv():
        ranking = get_photographer_ranking()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Фотограф", "Количество позиций", "Выручка"])
        for row in ranking:
            writer.writerow([row["full_name"], row["items_sold"], f"{row['revenue']:.2f}"])
        return csv_response(output.getvalue(), "photographer_stats.csv")

    @app.context_processor
    def inject_globals():
        return {
            "nav_items": NAV_ITEMS,
            "display_name": display_name,
            "today_iso": date.today().isoformat(),
            "status_label": status_label,
        }

    @app.template_filter("currency")
    def currency_filter(value):
        value = value or 0
        return f"{float(value):,.0f} ₽".replace(",", " ")

    @app.template_filter("shortdate")
    def shortdate_filter(value):
        if not value:
            return "—"
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")

    init_db(force=False)
    return app



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


app = create_app()
application = app


if __name__ == "__main__":
    app.run(debug=True)
