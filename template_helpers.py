from datetime import date, datetime

from config import NAV_ITEMS
from users import display_name, status_label


def register_template_helpers(app):
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
