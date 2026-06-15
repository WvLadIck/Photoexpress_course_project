from flask import abort, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required

from config import ROLE_ADMIN, ROLE_PHOTOGRAPHER
from db import get_db, query_all, query_one
from report_services import (
    apply_stock_delta,
    build_pie_svg,
    calculate_item_deltas,
    compute_totals,
    delete_file_if_exists,
    ensure_report_access,
    ensure_stock_available,
    get_active_products,
    get_report,
    get_report_attachments,
    get_report_comments,
    get_report_item_quantity_map,
    get_report_items,
    parse_report_form,
    replace_report_items,
    save_report_items,
    save_uploaded_files,
)
from users import role_required


def register_report_routes(app):
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
