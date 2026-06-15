import csv
import io

from flask_login import current_user, login_required

from config import ROLE_ADMIN
from db import query_all
from report_services import csv_response, get_photographer_ranking
from users import display_name, role_required


def register_export_routes(app):
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
