"""生成时入库（`knowledge/remember.py`）与 `sources` 帧的行为约束。

三组，各自钉一类错误：

1. **id 换得对**。入库之后 `Source.id` 必须变成库里的持久 id，且映射表里
   旧 id → 新 id 一一对应。换错的后果不是报错，而是导图里的引用重新打开后
   指不到原文——用户看到"引用在，点开没东西"。
2. **单份失败不拖垮整次生成**。入库是记账，不是生成的前置条件：一份材料写
   不进去，这一轮照样要能生成，只是它下次不能从库里复用。
3. **开关关掉时逐字段不变**。`MINDMAP_LIBRARY_REMEMBER=0` 必须是"完全不碰库"，
   而不是"碰了但结果一样"——线上那一刻没法调库，碰了就是投递失败。

全部离线：`conftest.py` 已清空凭证、开关置 0，数据库落在本次用例的 `tmp_path`。
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import library, remember


def _source(text: str, *, filename: str | None = None) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, filename=filename)


def test_ids_are_replaced_with_persistent_ones(tmp_path):
    first = _source("# 第一章\n\n正文若干。", filename="第一章.md")
    second = _source("# 第二章\n\n另一段正文。", filename="第二章.md")

    result = remember.remember_sources([first, second])

    # 两份都进了库，id 全换掉（持久 id 是入库时生成的 uuid，靠 content_hash 复用）。
    ids = [source.id for source in result.sources]
    assert ids[0] != first.id and ids[1] != second.id
    assert result.mapping == {first.id: ids[0], second.id: ids[1]}
    # 库里确实有这两条，且正文没变。
    assert library.load(ids[0]).text == first.text
    assert library.load(ids[1]).text == second.text
    assert len(library.list_sources()) == 2


def test_other_fields_are_untouched(tmp_path):
    """换 id 只该换 id：文件名、元数据、逐句时间戳都不许动。"""
    source = Source(
        kind=SourceKind.markdown,
        status="parsed",
        filename="会议纪要.md",
        text="# 会议\n\n决定下周发布。",
        parsed_pages=3,
        truncated=True,
        failed_pages=[2],
    )

    kept = remember.remember_sources([source]).sources[0]

    assert kept.filename == source.filename
    assert kept.kind == source.kind
    assert kept.parsed_pages == 3
    assert kept.truncated is True
    assert kept.failed_pages == [2]


def test_same_text_twice_reuses_one_record(tmp_path):
    """两份内容相同的材料（不同文件名）只该在库里占一条，id 也相同。"""
    first = _source("同一份正文。", filename="甲.md")
    second = _source("同一份正文。", filename="乙.md")

    result = remember.remember_sources([first, second])

    assert result.sources[0].id == result.sources[1].id
    # 两个旧 id 都指向同一个持久 id：前端按旧 id 换都要能换到。
    assert len(set(result.mapping.values())) == 1
    assert len(library.list_sources()) == 1


def test_blank_material_keeps_its_id_and_does_not_raise(tmp_path):
    """空正文不进库（`library.ingest` 会抛 ValueError），但不能因此中断整批。"""
    blank = _source("   \n  ", filename="空.md")
    usable = _source("有正文。", filename="有.md")

    result = remember.remember_sources([blank, usable])

    assert result.sources[0].id == blank.id          # 保留原 uuid
    assert result.sources[1].id != usable.id          # 换成了持久 id
    assert blank.id not in result.mapping
    assert len(library.list_sources()) == 1


def test_library_failure_does_not_break_the_batch(tmp_path, monkeypatch):
    """库写不进去时整批保留原 id：生成照跑，只是这批材料不能复用。"""
    def boom(source):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(library, "ingest", boom)
    sources = [_source("甲。"), _source("乙。")]

    result = remember.remember_sources(sources)

    assert result.sources == sources
    assert result.mapping == {}


def test_unexpected_error_is_contained_and_logged(tmp_path, monkeypatch, caplog):
    """不在已知清单里的意外同样不许穿透：入库是旁路，没有拖垮生成的权力。

    现实里的意外不止 `ValueError`/`sqlite3.Error`/`OSError`：旧记录的 metadata
    过不了 pydantic 校验、编码器内部状态不对，都可能在这里冒头。异常一旦穿到
    `agent/router.py` 的兜底分支，整轮生成就变成 `agent_failed`——一份材料的
    记账失败不该有这个后果。
    """
    def boom(source):
        raise RuntimeError("意外：编码器内部状态不对")

    monkeypatch.setattr(library, "ingest", boom)
    sources = [_source("甲。"), _source("乙。")]

    with caplog.at_level(logging.ERROR, logger=remember.__name__):
        result = remember.remember_sources(sources)

    assert result.sources == sources
    assert result.mapping == {}
    # 必须带 exc_info：否则线上只知道"没入进去"，不知道是哪一层炸的。
    logged = [record for record in caplog.records if "材料没能进知识库" in record.getMessage()]
    assert logged and all(record.exc_info for record in logged)


def test_switch_off_does_not_touch_the_library(tmp_path, monkeypatch):
    monkeypatch.setenv(remember.REMEMBER_ENV, "0")
    source = _source("不该进库的正文。")

    result = remember.remember_sources([source])

    assert result.sources == [source]
    assert result.mapping == {}
    assert library.list_sources() == []


def test_no_sources_is_a_no_op(tmp_path):
    result = remember.remember_sources([])

    assert result.sources == []
    assert result.mapping == {}
    assert library.list_sources() == []


@pytest.mark.parametrize("value,expected", [("0", False), ("1", True), ("", True), ("  ", True), ("false", True)])
def test_switch_only_recognises_zero(monkeypatch, value, expected):
    """开关只认 `"0"`：写 `false` 之类的值不算关，否则线上会莫名其妙不写库。"""
    monkeypatch.setenv(remember.REMEMBER_ENV, value)

    assert remember.remember_enabled() is expected
