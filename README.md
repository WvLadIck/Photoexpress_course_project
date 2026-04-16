# ФотоЭкспресс — курсовой проект на Flask

Веб-приложение для сбора, проверки и анализа отчётов о продажах фотографий.

## Что реализовано

- аутентификация и авторизация пользователей;
- разграничение прав доступа по ролям `admin` и `photographer`;
- CRUD для отчётов, фотографов и расходных материалов;
- агрегирование данных по продажам и выручке;
- загрузка файлов и CSV-выгрузка;
- несколько связанных сущностей: `users`, `reports`, `products`, `report_items`, `report_attachments`, `report_comments`.

## Стек

- Python
- Flask
- Jinja2
- sqlite3
- Flask-Login
- Werkzeug

## Быстрый запуск

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
flask --app app init-db
flask --app app run --debug
```

Либо:

```bash
python app.py
```

## Тестовые учетные записи

- Администратор: `admin` / `admin123`
- Фотограф 1: `photo1` / `photo123`
- Фотограф 2: `photo2` / `photo123`

## Что показывать на защите

- вход по ролям и разный интерфейс;
- создание отчёта с файлами;
- проверка отчёта администратором и отправка комментария;
- автоматическое обновление остатков материалов;
- CSV-экспорт и агрегированную статистику.
