"""外部检索的代理取值：显式配置优先，落到兜底值时必须留痕。

这一组只测 `external._proxy()` 这一件事。它值得单独一组，因为
`DEFAULT_PROXY = "http://127.0.0.1:7897"` 是两个环境里含义相反的值：

  - 开发机上它是对的（Clash Verge 就在 7897）；
  - 服务器上它是错的（没人监听那个端口），而五个源会全部 `ConnectError`，
    上层只看到"这次没搜到外部资料"——**坏了和本来就没有长得一样**。

所以判据不只是"值对不对"，还有"走到兜底时有没有留痕、且只留一次"。
"""
from __future__ import annotations

import logging

import pytest

from app.knowledge import external

_PROXY_NAMES = (
    external.EXTERNAL_PROXY_ENV,
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """清掉五个来源，并复位"已经告警过"这个模块级标记。

    不复位的话用例会互相影响：`_WARNED_DEFAULT_PROXY` 是模块级的一次性标记，
    前一条用例写过之后，后面断言"这里应该 warning"的那条就永远收不到。
    """
    for name in _PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(external, "_WARNED_DEFAULT_PROXY", False)


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.levelno >= logging.WARNING
            and record.name == external.logger.name]


def test_the_explicit_variable_wins(monkeypatch):
    monkeypatch.setenv(external.EXTERNAL_PROXY_ENV, "http://127.0.0.1:7890")
    assert external._proxy() == "http://127.0.0.1:7890"


def test_the_explicit_variable_beats_the_ambient_ones(monkeypatch):
    """`MINDMAP_EXTERNAL_PROXY` 优先于 `HTTPS_PROXY`。

    这条不只是顺序偏好：`HTTPS_PROXY` 是全局的，拿它当外部检索的配置会让
    "别人在这台机器上装了个代理"变成"我们的外部检索改了目标"。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv(external.EXTERNAL_PROXY_ENV, "http://127.0.0.1:7890")
    assert external._proxy() == "http://127.0.0.1:7890"


def test_an_ambient_proxy_is_still_honoured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
    assert external._proxy() == "http://10.9.9.9:3128"


@pytest.mark.parametrize("value", ["none", "off", "direct", "0", "NONE", " Off "])
def test_direct_is_a_first_class_answer(monkeypatch, value):
    """`none` / `off` / `direct` / `0` 表示显式直连，大小写与首尾空白都不计较。

    没有这个出口的话，"机器上本来就有透明代理"的环境里关不掉它，
    而那正好是我们最需要能关掉的场合（排查"到底是源挂了还是代理挂了"）。
    """
    monkeypatch.setenv(external.EXTERNAL_PROXY_ENV, value)
    assert external._proxy() is None


def test_falling_back_to_the_guess_is_said_out_loud(caplog):
    """什么都没配时用兜底值，并且**说一句**。

    这句话是这条链上唯一的"降过级"痕迹：没有它，线上看到的永远是
    "外部源都没返回内容"，而真实原因是没人监听 7897。
    """
    with caplog.at_level(logging.WARNING, logger=external.logger.name):
        assert external._proxy() == external.DEFAULT_PROXY

    messages = _warnings(caplog)
    assert len(messages) == 1
    assert external.EXTERNAL_PROXY_ENV in messages[0]
    assert external.DEFAULT_PROXY in messages[0]


def test_the_guess_is_said_only_once(caplog):
    """重复走兜底只告警一次。

    `_proxy()` 在每次外部检索里都会被调用，逐次 warning 会把真正的错误淹掉
    （这也正是"日志要少而准"的代价：第一次之后就没有痕迹了，所以第一次
    必须把话说全）。
    """
    with caplog.at_level(logging.WARNING, logger=external.logger.name):
        for _ in range(5):
            assert external._proxy() == external.DEFAULT_PROXY

    assert len(_warnings(caplog)) == 1


def test_an_explicit_value_never_warns(monkeypatch, caplog):
    monkeypatch.setenv(external.EXTERNAL_PROXY_ENV, "http://127.0.0.1:7890")
    with caplog.at_level(logging.WARNING, logger=external.logger.name):
        external._proxy()
    assert _warnings(caplog) == []


def test_direct_never_warns(monkeypatch, caplog):
    monkeypatch.setenv(external.EXTERNAL_PROXY_ENV, "none")
    with caplog.at_level(logging.WARNING, logger=external.logger.name):
        external._proxy()
    assert _warnings(caplog) == []


def test_the_warning_names_the_way_out(caplog):
    """告警里必须给出"怎么改"，而不是只说"我猜了一个值"。

    这条断言盯的是文案质量：读到这句的人未必知道 `MINDMAP_EXTERNAL_PROXY`
    是干什么的，所以消息里要同时出现两个出路——显式配一个地址，或设成
    `none` 表示直连。
    """
    with caplog.at_level(logging.WARNING, logger=external.logger.name):
        external._proxy()

    message = _warnings(caplog)[0]
    assert "显式设置" in message
    assert "none" in message
    assert "直连" in message
