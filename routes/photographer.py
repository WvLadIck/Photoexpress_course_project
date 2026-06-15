from flask import render_template
from flask_login import current_user, login_required

from config import ROLE_PHOTOGRAPHER
from db import query_all
from report_services import build_pie_svg, get_summary_for_photographer
from users import role_required


def register_photographer_routes(app):
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
