# backend/forms.py

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError

# ▼ models.py から User モデルをインポート ▼
# (メールアドレスの重複チェックで使用するため)
from backend.models import User

class RegistrationForm(FlaskForm):
    """ユーザー登録フォーム"""
    email = StringField('メールアドレス', validators=[
        DataRequired(message="メールアドレスは必須です。"),
        Email(message="有効なメールアドレスを入力してください。")
    ])
    password = PasswordField('パスワード', validators=[
        DataRequired(message="パスワードは必須です。")
    ])
    password2 = PasswordField(
        'パスワードの確認', validators=[
            DataRequired(message="パスワードの確認は必須です。"),
            EqualTo('password', message="パスワードが一致しません。") # 'password'フィールドの値と一致するか
        ])
    submit = SubmitField('登録する')

    # カスタムバリデーション: メールアドレスが既に使われていないかチェック
    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user is not None:
            raise ValidationError('このメールアドレスは既に使用されています。')

class LoginForm(FlaskForm):
    """ログインフォーム"""
    email = StringField('メールアドレス', validators=[
        DataRequired(message="メールアドレスは必須です。"),
        Email(message="有効なメールアドレスを入力してください。")
    ])
    password = PasswordField('パスワード', validators=[
        DataRequired(message="パスワードは必須です。")
    ])
    remember_me = BooleanField('ログイン状態を保持する') # チェックボックス
    submit = SubmitField('ログイン')