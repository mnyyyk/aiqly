# backend/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# インスタンスをここで作成
db = SQLAlchemy()
login_manager = LoginManager()