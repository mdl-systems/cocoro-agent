"""pytest conftest — テスト実行前に環境変数を確実にセット"""
import os

# テスト用環境変数を import より前にセットする（conftest は最初に読まれる）
os.environ["COCORO_API_KEY"]  = "test-key-123"
os.environ["DATABASE_URL"]    = "postgresql://cocoro:cocoro_secret@localhost:5432/cocoro_db"
os.environ["REDIS_URL"]       = "redis://localhost:6379/0"
