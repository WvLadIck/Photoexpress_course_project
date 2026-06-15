from flask_login import LoginManager

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Сначала выполните вход."
login_manager.login_message_category = "warning"
