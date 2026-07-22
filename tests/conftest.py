"""pytest 共用設定：把 repo 根目錄放進 sys.path，讓 `from kktix_api import ...` 可 import；
提供 fixtures 目錄 helper。"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(*parts) -> str:
    """讀 tests/fixtures/ 底下的檔案內容（找不到回空字串，讓測試自行 skip）。"""
    path = os.path.join(FIXTURES, *parts)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
