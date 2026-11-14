
from app.constants import COMMIT_MODE_PER_BATCH, COMMIT_MODES
from app.extensions import db

COMMIT_MODE_KEY = "commit_mode"


class AppSetting(db.Model):
    __tablename__ = "app_setting"

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)

    def __repr__(self):
        return f"<AppSetting {self.key}={self.value}>"


def get_setting(key, default=None):
    row = db.session.get(AppSetting, key)
    return row.value if row else default


def set_setting(key, value):
    row = db.session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    return row


def get_commit_mode():
    mode = get_setting(COMMIT_MODE_KEY, COMMIT_MODE_PER_BATCH)
    return mode if mode in COMMIT_MODES else COMMIT_MODE_PER_BATCH


def set_commit_mode(mode):
    if mode not in COMMIT_MODES:
        raise ValueError(f"Unknown commit mode: {mode}")
    return set_setting(COMMIT_MODE_KEY, mode)
