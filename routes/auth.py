from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from db import query_one
from users import AppUser, is_safe_next_url, redirect_for_role


def register_auth_routes(app):
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
