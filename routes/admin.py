from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from config import ROLE_ADMIN, ROLE_PHOTOGRAPHER
from db import get_db, query_all, query_one
from report_services import (
    build_pie_svg,
    get_active_products,
    get_global_summary,
    get_photographer,
    get_photographer_ranking,
    get_product,
    get_report,
    get_report_attachments,
    get_report_comments,
    get_report_items,
    get_summary_for_photographer,
    parse_float,
    parse_int,
    parse_photographer_form,
)
from users import display_name, get_role_id, role_required


def register_admin_routes(app):
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
