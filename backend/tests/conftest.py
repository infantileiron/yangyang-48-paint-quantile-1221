import os
import sys
from pathlib import Path

# 在导入应用模块前指定一个文件型 SQLite，保证测试不依赖 MySQL/Redis。
_TMP_DB = Path(__file__).resolve().parent / '.pytest-session.db'
if _TMP_DB.exists():
    _TMP_DB.unlink()
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP_DB}')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/15')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('JWT_SECRET', 'pytest-secret')

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from src.models.base import Base, Session, engine  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    with Session.begin() as session:
        yield session
    Base.metadata.drop_all(engine)
