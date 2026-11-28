"""`scripts/migrate_sqlite_to_pg.py` 把 SQLite 快照搬进 PG。

这个脚本上线时真的跑过一次生产数据，而在那之前它是**零测试覆盖**的——第一条真实
失败就是它带出来的：`library_sources` 里有一格正文含 NUL（`0x00`），SQLite 的 TEXT
允许、PG 的 text 不允许，于是整批 `INSERT` 抛
`psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`，
前 14 张表已经搬进去了、这张卡住，服务停着等人。

所以这里钉三件事：
  - 含 NUL 的快照能完整搬完（折成空格，不是丢掉那行、也不是报错退出）；
  - 折了几处要进报表（静默改写一份用户材料不能只写进日志）；
  - 折完 `_verify` 仍然全一致（行数、主键、`content` 逐字节）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "migrate_sqlite_to_pg.py"

#: 老库（SQLite 时代）的建表语句。`library_chunks` 有外键，顺带把"父表先搬"的顺序
#: 也一起验了；`mindmaps` 必须有——`main()` 结尾无条件打印 `content` 逐字节比对了
#: 多少行，老库里没有这张表会直接 `no such table`（真实快照里它一定在）。
LEGACY_DDL = """
CREATE TABLE mindmaps (
    id         TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE library_sources (
    id            TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    filename      TEXT,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    metadata      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    UNIQUE(content_hash)
);
CREATE TABLE library_chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL,
    start      INTEGER NOT NULL,
    "end"      INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    parent     INTEGER,
    UNIQUE(source_id, start, "end", level)
);
"""

#: 那一格真的坏过的正文形态：中文夹一个 NUL，前后都是有意义的字。
DIRTY_TEXT = "思维导图（Mind Map）又叫心智图，依据\x00大脑放射性思维的特点"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_pg", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_snapshot(path: Path, *, text: str = DIRTY_TEXT) -> Path:
    """造一份最小的老库快照：一张导图、一条来源、它的两个切块。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_DDL)
        conn.execute(
            "INSERT INTO mindmaps (id, version, content, updated_at) VALUES (?,?,?,?)",
            ("map-1", 3, '{"title": "番茄炒蛋"}', "2026-11-01T00:00:00.000Z"))
        conn.execute(
            "INSERT INTO library_sources (id, content_hash, filename, kind, text, "
            "metadata, created_at, last_used_at) VALUES (?,?,?,?,?,?,?,?)",
            ("src-1", "0" * 64, "baike: 思维导图", "web", text, "{}",
             "2026-11-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z"))
        conn.executemany(
            'INSERT INTO library_chunks (source_id, start, "end", level, parent) '
            "VALUES (?,?,?,?,?)",
            [("src-1", 0, 10, 0, None), ("src-1", 0, 10, 1, 0)])
        conn.commit()
    finally:
        conn.close()
    return path


def _pg_rows(module, table: str, columns: str) -> list[dict]:
    # `""` 走"没给 --dsn"那条分支：DSN 从 `MINDMAP_PG_DSN` 读，`clean_env` 已经把它
    # 指到测试库了（`_resolve_dsn` 对 None 会 `AttributeError`，它接受的是字符串）。
    conn = module._pg_connect(module._resolve_dsn(""))
    try:
        return [dict(row) for row in conn.execute(
            f"SELECT {columns} FROM {module._quote(table)}").fetchall()]
    finally:
        conn.close()


@pytest.fixture()
def snapshot(tmp_path: Path) -> Path:
    return _make_snapshot(tmp_path / "mindmaps.db")


def test_dirty_text_is_folded_instead_of_crashing(snapshot: Path):
    """NUL 折成空格搬进来，切块也跟着搬——不是跳过那一行。

    跳过是最诱人的"让它跑完"办法，代价是用户的一条材料在升级后凭空消失，而
    `_verify` 报的"行数不等"会被当成库里有脏数据。
    """
    module = _load_script()
    assert module.main(["--source", str(snapshot), "--skip-schema"]) == 0

    sources = _pg_rows(module, "library_sources", "id, text, content_hash")
    assert len(sources) == 1
    assert "\x00" not in sources[0]["text"]
    assert sources[0]["text"] == DIRTY_TEXT.replace("\x00", " ")
    # 指纹是老库里的值，脚本不重算——重算会让"同一条来源"在新库里换一个身份。
    assert sources[0]["content_hash"] == "0" * 64

    chunks = _pg_rows(module, "library_chunks", "source_id, start, level")
    assert len(chunks) == 2
    assert {row["source_id"] for row in chunks} == {"src-1"}


def test_folding_is_reported_per_table(snapshot: Path, capsys):
    """折了几处要打在报表里：静默改写用户材料是不能接受的。

    备注列同时也是"这行不是原样搬过来的"的唯一线索——出问题时运维靠它知道去哪
    张表、哪几个字符上找。
    """
    module = _load_script()
    assert module.main(["--source", str(snapshot), "--skip-schema"]) == 0

    out = capsys.readouterr().out
    assert "折控制字符 1 处" in out, "折了 1 格正文，报表里必须写明"


def test_clean_text_is_not_reported_as_folded(tmp_path: Path, capsys):
    """干净正文不该出现在"折控制字符"备注里：那会让人以为每次迁移都在改数据。"""
    module = _load_script()
    snapshot = _make_snapshot(tmp_path / "clean.db", text="干净的正文，没有任何控制字符。")

    assert module.main(["--source", str(snapshot), "--skip-schema"]) == 0

    out = capsys.readouterr().out
    assert "折控制字符" not in out
    assert _pg_rows(module, "library_sources", "text")[0]["text"] == "干净的正文，没有任何控制字符。"


def test_rerun_after_folding_does_not_duplicate(snapshot: Path):
    """折字符之后重跑仍然幂等：唯一键没变，行不该翻倍。

    生产上第一次跑是崩的，修完脚本就地重跑——那条路径必须干净，否则"重跑续上"
    这个说法就不成立了。
    """
    module = _load_script()
    assert module.main(["--source", str(snapshot), "--skip-schema"]) == 0
    assert module.main(["--source", str(snapshot), "--skip-schema"]) == 0

    assert len(_pg_rows(module, "library_sources", "id")) == 1
    assert len(_pg_rows(module, "library_chunks", "id")) == 2


def test_dry_run_touches_neither_target_nor_snapshot(snapshot: Path, capsys):
    """`--dry-run` 只读老库：目标库一行都不多，老库文件一字节都不变。

    这条路径的契约是"不连目标库"（所以脚本故意不 import `app.db.pool`），而它常
    被用来在生产机上先看一眼——看的时候把库改了就太糟了。
    """
    module = _load_script()
    before = snapshot.read_bytes()

    assert module.main(["--source", str(snapshot), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "library_sources" in out and "2" in out
    assert snapshot.read_bytes() == before
    assert _pg_rows(module, "library_sources", "id") == []
