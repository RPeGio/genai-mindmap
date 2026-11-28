"""`app/core/textfold` 的单元测试：零依赖那一条是硬约束，值得直接钉住。

`fold_control_chars` 有两个调用方，其中一个是 `scripts/migrate_sqlite_to_pg.py`——
那个脚本会在"只有 psycopg、没有 pydantic"的临时环境里跑（在 ECS 上实测撞过
`ModuleNotFoundError: No module named 'pydantic'`）。所以这里除了行为，还断言模块
本身不引入任何第三方依赖。
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from app.core import textfold
from app.core.textfold import KEPT_CONTROLS, fold_control_chars

BACKEND_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = BACKEND_DIR / "app" / "core" / "textfold.py"


def test_nul_becomes_a_space_not_nothing():
    """`abc\\x00def` 是两个词。删掉会拼成一个不存在的词，空格才对。"""
    assert fold_control_chars("abc\x00def") == "abc def"


def test_the_real_world_cell_from_the_ecs_migration():
    """ECS 上真撞到的那一条（`library_sources` 里 NUL 在下标 50）。

    这里把整句留在测试里：它同时是"为什么要有这个函数"的可执行记录。
    """
    dirty = "思维导图（Mind Map）又叫心智图，是英国心理学家、教育专家东尼·博赞(TonyBuzan)依据\x00大脑放射性思维的特点发明的"
    folded = fold_control_chars(dirty)

    assert "\x00" not in folded
    assert folded == dirty.replace("\x00", " ")
    assert folded.index("依据") < folded.index("大脑")


@pytest.mark.parametrize("kept", sorted(KEPT_CONTROLS))
def test_kept_controls_survive(kept):
    """换行、制表、回车、换页必须原样保留——切块算法按段落切。"""
    assert fold_control_chars(f"a{kept}b") == f"a{kept}b"


@pytest.mark.parametrize("folded", ["\x00", "\x01", "\x07", "\x1b", "\x7f"])
def test_other_controls_are_folded(folded):
    """`\\x1b` 是 ANSI 转义的开头：留在正文里，导出的文档会带颜色。"""
    assert fold_control_chars(f"a{folded}b") == "a b"


def test_clean_text_is_returned_unchanged():
    text = "一、总则\n\n本制度规定费用支出。\n\n二、审批权限"
    assert fold_control_chars(text) == text


def test_empty_and_none_like_inputs():
    assert fold_control_chars("") == ""


def test_only_controls_folds_to_blank():
    """折完是空白，但**这个函数不判断空正文**——那是调用点的事。"""
    assert fold_control_chars("\x00\x01\x02").strip() == ""


def test_ast_imports_are_stdlib_only():
    """模块的 import 只能是标准库：这是 `migrate_sqlite_to_pg.py` 能 import 它的前提。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)

    assert imported, "至少要 import unicodedata"
    for name in imported:
        top = name.split(".")[0]
        assert top in sys.stdlib_module_names, f"{name} 不是标准库模块"
        assert top != "app", "这个模块不许 import 本仓其它模块（会拖进依赖链）"
