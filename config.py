from pathlib import Path

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
