"""模型设置：面板里改、立即生效、密钥不出门。

这个文件钉四件事，每件都对应一种"看起来能跑、实际上是错的"形状：

1. **回落顺序**：库里的值 > 环境变量 > 空。九条路径里只要有一条自己读环境变量，
   用户在面板里改完就会得到"生成换了、视觉提取没换"的半生效系统。
2. **密钥不出门**：`GET` / `PUT` 的响应只有打码值（`…abcd`），错误文案与日志里
   不出现明文。这不是防"有服务器权限的人"（他们能读 SQLite），是防截图、devtools
   的 Network 面板、以及出错时被拼进某条日志。
3. **两种「空」的语义不同**：`api_key` 的空串 = **不改**（界面显示打码值，用户
   不动它就提交空串）；`base_url` / `model` 的空串 = **清掉覆盖、回落环境变量**。
   要清密钥必须显式 `clear_api_key=true`。
4. **测试连接是一次真请求的形状**：`max_tokens` 极小、失败按状态码给可读中文、
   200 但正文不是 JSON 也要判失败（有的端点 404 会伪装成 200）。

单测不发真网络：`tests/conftest.py` 的 `no_real_http` 会把传输层换成断言失败，
所以这里替换 `probe._client()` 造一个假客户端。
"""
from __future__ import annotations

import json
import logging
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import register_error_handlers
from app.generation import service as generation_service
from app.generation.service import DEEP_MODEL_FALLBACK, ModelNotConfigured
from app.ingestion import vision_pdf
from app.mindmaps import repository
from app.settings import probe as probe_module
from app.settings import store
from app.settings.router import router as settings_router

api = FastAPI()
register_error_handlers(api)
api.include_router(settings_router, prefix="/api")
client = TestClient(api)

#: 一把形状像真的密钥。用例里到处断言它**没有**出现在响应/日志里。
SECRET = "sk-live-abcdefghijklmnop-1234"
SECRET_TAIL = "1234"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个用例都把两个进程级全局量复位。

    `_DEGRADED_WARNED`：不复位的话，前一个用例制造的读失败会让后一个用例看不到
    本该出现的告警——"证明降级会留痕"那条用例就会随机绿。

    `_CACHE`：读缓存跨用例存活。键里带了库路径，正常情况下不会串，但"直接写库再
    读"的用例会撞上自己上一次读过的旧值——那种失败读起来像"保存没生效"。
    """
    store._DEGRADED_WARNED = False
    store._CACHE.clear()
    yield
    store._DEGRADED_WARNED = False
    store._CACHE.clear()


def _body(**kwargs) -> dict:
    return kwargs


# ---------------------------------------------------------------- 读

def test_empty_config_is_not_configured_and_has_no_mask():
    """什么都没配：`configured=false`，打码值是空串而不是一个假的 `…`。"""
    body = client.get("/api/settings/model").json()
    assert body["configured"] is False
    assert body["base_url"] == ""
    assert body["api_key_masked"] == ""
    assert body["api_key_set"] is False
    assert body["base_url_source"] == store.SOURCE_UNSET
    assert body["api_key_source"] == store.SOURCE_UNSET
    assert body["model_source"] == store.SOURCE_UNSET
    assert body["updated_at"] is None


def test_saved_key_is_masked_down_to_the_last_four():
    """`GET` 只给打码值，且只露末 4 位。**整串不出现在响应文本里**。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    response = client.get("/api/settings/model")
    body = response.json()
    assert SECRET not in response.text
    assert body["api_key_masked"] == "…" + SECRET_TAIL
    assert body["api_key_masked"].endswith(SECRET_TAIL)
    assert body["api_key_set"] is True
    # 打码值里不能带供应商前缀：那只是命名习惯，露出来对用户没信息量。
    assert "sk-" not in body["api_key_masked"]


def test_short_key_is_fully_masked():
    """8 位以内的口令只给 `…`：露末 4 位等于送出去一半以上。"""
    client.put("/api/settings/model", json=_body(api_key="abc123"))
    assert store.mask_api_key("abc123") == "…"
    assert client.get("/api/settings/model").json()["api_key_masked"] == "…"


def test_put_response_does_not_leak_the_key_either():
    """`PUT` 的响应与 `GET` 同一形状，同样只有打码值。"""
    response = client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    assert response.status_code == 200
    assert SECRET not in response.text
    assert response.json()["api_key_masked"] == "…" + SECRET_TAIL


# ---------------------------------------------------------------- 写：立即生效

def test_put_takes_effect_without_a_restart():
    """**这条是整张卡的核心**：`PUT` 之后 `_config()` 立刻拿到新值。

    `_config()` 不走缓存，每次重新解析——所以不需要重启，也就不会打断正在跑的
    长图。断言打的是"所有出口共用的那个函数"，不是某个中间变量。
    """
    assert store.resolve().base_url == ""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))

    base, key, model = generation_service._config()
    assert (base, key, model) == ("https://api.deepseek.com/v1", SECRET, "deepseek-chat")


def test_trailing_slash_on_base_url_is_normalised():
    """`https://x/v1/` 会打到 `//chat/completions`；少数端点直接 404，而 404 只能
    读成"模型名不对"。规范化与改动前逐字节一致。"""
    client.put("/api/settings/model", json=_body(base_url="https://x.example/v1/"))
    assert store.resolve().base_url == "https://x.example/v1"


def test_saving_identical_values_reports_unchanged():
    """同值重复保存不刷 `updated_at`，并如实回 `changed=false`。

    否则界面上会出现一条"刚刚改过"、其实一直没变的记录。
    """
    payload = _body(base_url="https://api.deepseek.com/v1", api_key=SECRET,
                    model="deepseek-chat")
    first = client.put("/api/settings/model", json=payload).json()
    assert first["changed"] is True
    stamp = first["updated_at"]
    second = client.put("/api/settings/model", json=payload).json()
    assert second["changed"] is False
    assert second["updated_at"] == stamp


def test_empty_api_key_means_keep_the_saved_one():
    """**语义 1**：界面上那个框显示打码值，用户不动它就提交空串 → 不改。

    把空串当"清空"，等于每次改个模型名就把密钥抹掉一次。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    response = client.put("/api/settings/model", json=_body(api_key="", model="deepseek-chat"))
    assert response.json()["api_key_set"] is True
    assert store.saved().api_key == SECRET


def test_posting_the_masked_key_back_is_rejected_not_saved():
    """**语义 1 的第三条边界**：把界面上看到的打码值原样提交，不许当密钥存。

    界面把 `…1234` 放在 placeholder 里，用户不填就不会提交它；但**接口**不设防时，
    任何直接调后端的人（或将来把 placeholder 换成 value 的改动）都会踩到：库里
    那把真密钥被字面量 `…1234` 顶掉，而响应照旧 `api_key_set: true`、来源写
    "你在面板里设的"——之后每一次模型调用都 401，界面却显示"刚配好"。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    masked = client.get("/api/settings/model").json()["api_key_masked"]
    assert masked == "…" + SECRET_TAIL

    response = client.put("/api/settings/model", json=_body(api_key=masked))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_api_key_masked"
    # 真的没被改：库里的密钥还是原来那把。
    assert store.saved().api_key == SECRET


def test_posting_the_mask_of_an_env_key_is_rejected_too(monkeypatch):
    """密钥只在环境变量里时，打码值来自环境变量——这一段最疼，也必须挡住。

    只比"库里那一行"会漏掉这条路：库是空的，`mask_api_key("")` 是空串，判据
    不成立；于是 `…1234` 被存进库，**顶掉了环境变量里那把真密钥**，而且
    `api_key_source` 从 `env` 翻成 `saved`——用户以为"我什么都没改"。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, SECRET)
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    masked = client.get("/api/settings/model").json()["api_key_masked"]
    assert masked == "…" + SECRET_TAIL
    assert store.saved().api_key == ""

    response = client.put("/api/settings/model", json=_body(api_key=masked))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_api_key_masked"
    # 环境变量那把仍然是生效值，来源还是 env。
    assert store.resolve().api_key == SECRET
    assert store.resolve().api_key_source == store.SOURCE_ENV


def test_clear_api_key_requires_the_explicit_flag():
    """**语义 1 的另一半**：要清密钥必须显式 `clear_api_key: true`。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    body = client.put("/api/settings/model", json=_body(clear_api_key=True)).json()
    assert body["api_key_set"] is False
    assert body["api_key_masked"] == ""
    assert store.saved().api_key == ""


def test_clear_and_new_key_together_is_rejected():
    """矛盾请求宁可 400：静默让其中一个赢，下一次就会有人在这里丢掉一把刚填的密钥。"""
    response = client.put("/api/settings/model", json=_body(
        api_key=SECRET, clear_api_key=True))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_clear_conflict"


def test_empty_base_url_clears_the_override_and_falls_back_to_env(monkeypatch):
    """**语义 2**：`base_url` 的空串 = 清掉库里的覆盖、回落到环境变量。

    「回到环境变量那套配置」是真实需求（换部署、回滚、临时排查），与"不改"是
    两件事。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    client.put("/api/settings/model", json=_body(base_url="https://panel.example/v1"))
    assert store.resolve().base_url == "https://panel.example/v1"
    assert store.resolve().base_url_source == store.SOURCE_SAVED

    body = client.put("/api/settings/model", json=_body(base_url="")).json()
    assert body["base_url"] == "https://env.example/v1"
    assert body["base_url_source"] == store.SOURCE_ENV


def test_invalid_base_url_is_rejected_with_a_readable_code():
    """写错一个协议头，现在就要说清楚——不要等到生成时报"模型服务不可用"。"""
    response = client.put("/api/settings/model", json=_body(base_url="api.deepseek.com/v1"))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_base_url_invalid"
    assert "https://" in response.json()["message"]


def test_unknown_field_is_rejected():
    """`extra="forbid"`：拼错字段名时宁可 422，也不要静默忽略。

    "我把密钥填进 `apikey` 了，保存成功但没生效"是最难自查的一类失败。
    """
    response = client.put("/api/settings/model", json=_body(apikey=SECRET))
    assert response.status_code == 422


# ---------------------------------------------------------------- 回落顺序

def test_env_is_the_fallback_when_nothing_is_saved(monkeypatch):
    """库里没有 → 环境变量。来源标成 `env`，界面据此显示「来自环境变量」。"""
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    body = client.get("/api/settings/model").json()
    assert (body["base_url"], body["model"]) == ("https://env.example/v1", "env-model")
    assert body["base_url_source"] == store.SOURCE_ENV
    assert body["model_source"] == store.SOURCE_ENV
    assert body["api_key_masked"] == "…0000"
    assert body["configured"] is True


def test_saved_value_wins_over_env(monkeypatch):
    """库 > 环境变量。反过来会让"面板里改完立刻生效"变成一个谎。"""
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    client.put("/api/settings/model", json=_body(
        base_url="https://panel.example/v1", model="panel-model"))
    resolved = store.resolve()
    assert (resolved.base_url, resolved.base_url_source) == (
        "https://panel.example/v1", store.SOURCE_SAVED)
    assert (resolved.model, resolved.model_source) == ("panel-model", store.SOURCE_SAVED)
    # 逐字段回落：只改了端点和模型名，密钥仍是环境变量里那把。
    assert (resolved.api_key, resolved.api_key_source) == ("env-key-0000", store.SOURCE_ENV)


def test_partial_save_keeps_the_other_fields(monkeypatch):
    """只填密钥是最常见的用法（换 key 比换供应商频繁得多），不能逼用户重填其余两项。"""
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    client.put("/api/settings/model", json=_body(api_key=SECRET))
    resolved = store.resolve()
    assert resolved.api_key == SECRET
    assert resolved.base_url == "https://env.example/v1"
    assert resolved.model == "env-model"
    assert resolved.api_key_source == store.SOURCE_SAVED
    assert resolved.base_url_source == store.SOURCE_ENV


def test_config_error_mentions_both_env_and_the_panel(monkeypatch):
    """缺配置时的话要同时指向两条出路：部署方改环境变量，使用者点面板。

    旧的文案只有"环境变量"——页面里出现了这个入口之后，那句话就不再是唯一的
    下一步了。`test_agent_loop` 里那条断言钉着"环境变量"这个词不能丢。
    """
    with pytest.raises(ModelNotConfigured) as caught:
        generation_service._config()
    message = str(caught.value)
    assert "环境变量" in message
    assert "模型设置" in message


# ---------------------------------------------------------------- 深度思考那一档

def test_explicit_deep_model_from_the_panel_reaches_model_for():
    """面板里填的深度模型名要对**所有**出口生效，不只是外层工具循环。

    `_model_for` 的三档在 `generation.service` 里，第 1 档（显式配置）走
    `store.deep_override()`——与其余三个字段同源。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        deep_model="my-reasoner"))
    assert generation_service._model_for(
        "https://api.deepseek.com/v1", "deepseek-chat", True) == "my-reasoner"
    # 不勾深度思考时仍然是原模型名——老路径不能漂移。
    assert generation_service._model_for(
        "https://api.deepseek.com/v1", "deepseek-chat", False) == "deepseek-chat"


def test_panel_deep_model_beats_the_env_var(monkeypatch):
    """库里的深度模型名高于 `MINDMAP_LLM_MODEL_DEEP`：用户刚在页面上改完。"""
    monkeypatch.setenv(store.DEEP_MODEL_ENV, "env-reasoner")
    client.put("/api/settings/model", json=_body(deep_model="panel-reasoner"))
    assert generation_service._model_for("https://api.deepseek.com", "deepseek-chat", True) == "panel-reasoner"


def test_clearing_deep_model_falls_back_to_the_heuristic(monkeypatch):
    """清掉显式值 → 回落到启发式（deepseek 端点 → `deepseek-reasoner`）。"""
    monkeypatch.delenv(store.DEEP_MODEL_ENV, raising=False)
    client.put("/api/settings/model", json=_body(deep_model="my-reasoner"))
    client.put("/api/settings/model", json=_body(deep_model=""))
    assert generation_service._model_for(
        "https://api.deepseek.com", "deepseek-chat", True) == DEEP_MODEL_FALLBACK


def test_deep_model_effective_is_reported_even_when_not_configured():
    """面板要显示"勾了深度思考实际会发哪个名字"，否则用户以为它没生效。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    body = client.get("/api/settings/model").json()
    assert body["deep_model"] == ""
    assert body["deep_model_source"] == store.SOURCE_UNSET
    assert body["deep_model_effective"] == DEEP_MODEL_FALLBACK


# ---------------------------------------------------------------- 视觉提取也走同一处

def test_vision_uses_the_panel_values(monkeypatch):
    """视觉提取原先自己读三个环境变量，于是面板改完只有生成那条路换了。

    这里钉的是"两条路同源"：库里有值、环境变量是别的值时，视觉拿到库里的。
    """
    monkeypatch.setenv(store.LLM_MODE_ENV, "model")
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    client.put("/api/settings/model", json=_body(
        base_url="https://panel.example/v1", api_key=SECRET, model="panel-model"))
    assert vision_pdf._config() == ("https://panel.example/v1", SECRET, "panel-model")


def test_vision_still_refuses_in_demo_mode(monkeypatch):
    """`MINDMAP_LLM_MODE` 仍是部署方的开关，面板改不动它——演示模式不许联网。"""
    monkeypatch.setenv(store.LLM_MODE_ENV, "demo")
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    with pytest.raises(vision_pdf.VisionUnavailable) as caught:
        vision_pdf._config()
    assert "demo" in str(caught.value)


def test_llm_mode_is_reported_read_only(monkeypatch):
    """面板要把 `demo` 显示出来：配好了却不生效，得有据可查。"""
    monkeypatch.setenv(store.LLM_MODE_ENV, "MODEL")
    assert client.get("/api/settings/model").json()["llm_mode"] == "model"


# ---------------------------------------------------------------- 测试连接

class _FakeResponse:
    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """假 httpx 客户端：记录请求，返回预设响应（或抛预设异常）。"""

    def __init__(self, *, response=None, exc=None, sent: list | None = None) -> None:
        self.response = response
        self.exc = exc
        self.sent = sent if sent is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, *, json=None, headers=None):
        self.sent.append({"url": url, "json": json, "headers": headers})
        if self.exc is not None:
            raise self.exc
        return self.response


def _use_fake(monkeypatch, **kwargs) -> list:
    """把 `probe._client` 换成假客户端，返回它记录的请求列表。"""
    sent: list = []
    fake = _FakeClient(sent=sent, **kwargs)
    monkeypatch.setattr(probe_module, "_client", lambda: fake)
    return sent


def test_probe_success_sends_one_tiny_request(monkeypatch):
    """成功路径：请求体是 `max_tokens=1` 的一次 ping，地址拼在 `/chat/completions`。"""
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": "p"}}],
    }))
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    body = client.post("/api/settings/model/test").json()

    assert body["ok"] is True
    assert body["model"] == "deepseek-chat"
    assert "连接正常" in body["message"]
    assert sent[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert sent[0]["json"]["max_tokens"] == 1
    assert sent[0]["json"]["model"] == "deepseek-chat"
    assert sent[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    # 打码值只有末 4 位 + 一次 ping：一次测试连接的花费是"一个 token"。
    assert sent[0]["json"]["messages"] == [{"role": "user", "content": "ping"}]


def test_probe_uses_the_unsaved_form_values(monkeypatch):
    """面板上的流程是"填 → 测 → 存"：请求体里的临时值优先于已生效值。

    不这样的话，"我改了地址但还没保存，想先验一下"就走不通。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": [{"message": {}}]}))
    client.post("/api/settings/model/test", json=_body(
        base_url="https://try.example/v1", api_key="try-key-9999", model="try-model"))
    assert sent[0]["url"] == "https://try.example/v1/chat/completions"
    assert sent[0]["json"]["model"] == "try-model"
    assert sent[0]["headers"]["Authorization"] == "Bearer try-key-9999"


def test_probe_empty_overrides_fall_back_to_effective_values(monkeypatch):
    """空字段 = 用当前生效值："我只改模型名"不必再手打一遍 key。"""
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": [{"message": {}}]}))
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    client.post("/api/settings/model/test", json=_body(model="other-model"))
    assert sent[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert sent[0]["json"]["model"] == "other-model"
    assert sent[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_probe_with_the_masked_key_is_refused_before_the_request(monkeypatch):
    """打码值不许拿去发真请求：上游会 401，界面就会把"你手里那把是好的"说成
    "密钥被拒"，用户于是去换一把本来没问题的密钥。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": [{"message": {}}]}))
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    masked = client.get("/api/settings/model").json()["api_key_masked"]

    response = client.post("/api/settings/model/test", json=_body(api_key=masked))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_api_key_masked"
    assert sent == []


def test_probe_rejects_a_wrong_key_with_a_readable_message(monkeypatch):
    """401 → "密钥被拒绝"，并且正文里**没有**明文 key。

    OpenAI 系列的 401 正文常常带一段 key 原文；不替换的话，用户一截图就漏了。
    """
    _use_fake(monkeypatch, response=_FakeResponse(
        401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}}))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat")).json()
    assert body["ok"] is False
    assert body["status"] == 401
    assert "密钥被拒绝" in body["message"]
    assert SECRET not in body["message"]


def test_probe_reports_a_wrong_model_name(monkeypatch):
    """404 同时可能是地址少 `/v1` 或模型名不对，文案得把两种都写上。"""
    _use_fake(monkeypatch, response=_FakeResponse(404, {"error": {"message": "model not found"}}))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="nope")).json()
    assert body["ok"] is False
    assert body["status"] == 404
    assert "/v1" in body["message"]
    assert "model not found" in body["message"]


def test_probe_treats_a_200_without_json_as_failure(monkeypatch):
    """有的兼容端点在路径不对时回 200 + 一页 HTML。那不是"连上了"。"""
    _use_fake(monkeypatch, response=_FakeResponse(200, None, text="<html>nope</html>"))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://not-openai.example/v1", api_key=SECRET, model="m")).json()
    assert body["ok"] is False
    assert "OpenAI 兼容" in body["message"]


def test_probe_treats_a_200_without_choices_as_failure(monkeypatch):
    """模型名不被端点认识时，有的服务回 200 + 空 choices——生成侧会变成
    "返回内容里没有可解析的 JSON"，用户毫无线索。

    文案要同时给两条线索：地址可能不是 OpenAI 兼容格式（面板允许自由填地址，
    这是不做 provider 下拉的代价），或者模型名不被认识。只写后者会把"地址填错"
    引到改模型名上去。
    """
    _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": []}))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="nope")).json()
    assert body["ok"] is False
    assert "OpenAI 兼容" in body["message"]
    assert "choices" in body["message"]


def test_probe_says_so_when_the_endpoint_is_not_openai_compatible(monkeypatch):
    """**非 OpenAI 兼容端点**的典型形状：`{"contents": [...]}`（Gemini 原生）。

    面板允许自由填地址（用户要的是这个），所以"填了一家原生协议"是必然会发生
    的输入，而不是异常输入。这条钉住那句话把用户指向地址那一行——生成阶段的
    "不是合法的 Chat Completions 响应"指不到地址。
    """
    _use_fake(monkeypatch, response=_FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
    }))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key=SECRET, model="gemini-2.0-flash")).json()
    assert body["ok"] is False
    assert body["status"] == 200
    assert "OpenAI 兼容" in body["message"]
    # 别把密钥漏进"服务端原话"里：这条响应没有 error 段，但断言成本很低。
    assert SECRET not in body["message"]


def test_probe_reports_a_timeout_as_a_sentence(monkeypatch):
    """超时不能冒成 500：那是"我填错了一个字符"被读成"服务器坏了"。"""
    _use_fake(monkeypatch, exc=httpx.ConnectTimeout("timed out"))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://slow.example/v1", api_key=SECRET, model="m")).json()
    assert body["ok"] is False
    assert "超时" in body["message"]


def test_probe_reports_a_connection_error_as_a_sentence(monkeypatch):
    _use_fake(monkeypatch, exc=httpx.ConnectError("name resolution failed"))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://nowhere.invalid/v1", api_key=SECRET, model="m")).json()
    assert body["ok"] is False
    assert "连不上" in body["message"]


def test_probe_with_incomplete_config_is_a_400():
    """三项不齐时连"测"都测不了——这是真的业务错误，走 400。"""
    response = client.post("/api/settings/model/test", json=_body())
    assert response.status_code == 400
    assert response.json()["code"] == "settings_incomplete"


# ---------------------------------------------------------------- 密钥不出门

def test_key_never_appears_in_any_response_body(monkeypatch):
    """把三个动词全走一遍，任何一次响应正文里都不得出现明文密钥。"""
    _use_fake(monkeypatch, response=_FakeResponse(500, {"error": {"message": "boom"}}))
    payload = _body(base_url="https://api.deepseek.com/v1", api_key=SECRET,
                    model="deepseek-chat")
    texts = [
        client.get("/api/settings/model").text,
        client.put("/api/settings/model", json=payload).text,
        client.post("/api/settings/model/test").text,
    ]
    for text in texts:
        assert SECRET not in text


def test_key_never_appears_in_the_log(monkeypatch, caplog):
    """日志里也不能有：容器日志/`journalctl` 经常被贴到工单里。

    覆盖两条会打日志的路径——面板读写与一次失败的测试连接。
    """
    _use_fake(monkeypatch, response=_FakeResponse(
        401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}}))
    caplog.set_level(logging.DEBUG)
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    client.get("/api/settings/model")
    client.post("/api/settings/model/test")
    for record in caplog.records:
        assert SECRET not in record.getMessage()
    assert SECRET not in caplog.text


def test_a_broken_db_degrades_to_env_instead_of_failing_generation(
        monkeypatch, tmp_path, caplog):
    """库读不出来时回落环境变量，并留一条 warning。

    这条路径在**每一次模型调用**的开头。让它因为一个读不到的配置表把整次生成
    打成失败，比"回落到环境变量"糟糕得多——后者在绝大多数部署上恰好是对的。
    "降级要留下降过级的痕迹"是 AGENTS.md 里那条约定。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "broken" / "nope.db"))
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "nope.db").write_text("not a database", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        base, key, model = generation_service._config()
    assert (base, key, model) == ("https://env.example/v1", "env-key-0000", "env-model")
    assert any("回落" in record.getMessage() for record in caplog.records)


def test_clear_resets_the_row():
    """`store.clear()` 只在用例与本地调试用：删掉行之后一切回到环境变量。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    assert store.clear() == 1
    assert store.resolve().base_url == ""


# ---------------------------------------------------------------- 建表

def test_table_is_created_by_the_repository_connect():
    """表由 `repository.connect()` 建：读它的路径在热路径上。

    多一个"发现表不存在 → 建表"的分支，只会在最不该出错的地方多一条可能出错的路径。
    """
    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert store.TABLE in names


def test_an_old_database_without_the_table_gets_it(monkeypatch, tmp_path):
    """**旧库**（线上那份 `data/mindmaps.db`）里没有 `model_settings` 表。

    这一步是真的会发生的：上线时 `MINDMAP_DB_PATH` 指向一个已经跑了几个月、里面有
    用户导图的 SQLite 文件。`connect()` 必须把它补上，而不是因为缺表把整次生成打成
    失败。这里造一个只含 `mindmaps` 表的旧库，走一遍 `connect()` → 写 → 读。
    """
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(str(path))
    try:
        # 旧库的最小形状：一张 users 会用的表 + 一个 `mindmaps` 表（不与新表重名）。
        legacy.execute("CREATE TABLE mindmaps (id TEXT PRIMARY KEY, payload TEXT)")
        legacy.execute("INSERT INTO mindmaps (id, payload) VALUES ('m1', '{}')")
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setenv("MINDMAP_DB_PATH", str(path))
    store._CACHE.clear()

    # connect() 之后表就存在了，且旧数据还在（补表不该动别的表）。
    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert store.TABLE in names
        assert conn.execute("SELECT COUNT(*) FROM mindmaps").fetchone()[0] == 1

    # 补出来的表可以正常读写：这是"迁移成功"的真正判据。
    saved, changed = store.save(base_url="https://api.deepseek.com/v1",
                                api_key=SECRET, model="deepseek-chat")
    assert changed is True
    assert saved.base_url == "https://api.deepseek.com/v1"
    assert store.saved().model == "deepseek-chat"
    # 再连一次仍然是同一行，不是每次 connect 插一行。
    with closing(repository.connect()) as conn:
        rows = conn.execute(f"SELECT id FROM {store.TABLE}").fetchall()
    assert [row[0] for row in rows] == [store.ROW_ID]


def test_reading_does_not_rebuild_the_table_on_every_call(monkeypatch):
    """一次生成里 `_config()` 会被调 5–27 次（实测，见 store 里那段注释）。

    每次读都走一遍 `repository.connect()`（它开头有十来个 `CREATE TABLE IF NOT
    EXISTS`）是白付的开销。这条用例钉两件事：**同一配置连读多次只真的查一次库**，
    以及**写完立刻能读到新值**（缓存不能把"保存即生效"变成"等一秒"）。
    """
    connects: list[int] = []
    original = store._connect

    def counting_connect():
        connects.append(1)
        return original()

    monkeypatch.setattr(store, "_connect", counting_connect)

    store.save(base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat")
    connects.clear()

    for _ in range(20):
        assert store.resolve().model == "deepseek-chat"
    # 上限是 1：`save()` 结尾已经读过一次，正常情形是 0 次（全命中缓存）。
    # 断言成 `== 1` 会把"缓存更有效"判成失败。
    assert len(connects) <= 1, f"20 次读打了 {len(connects)} 次库，缓存没生效"

    # 写入必须立刻让下一次读到新值，不能等 TTL。
    store.save(model="deepseek-reasoner")
    assert store.resolve().model == "deepseek-reasoner"


def test_the_ttl_is_short_enough_to_not_hide_a_change_from_another_worker(monkeypatch):
    """另一个进程改了库（本进程收不到 `_invalidate`）：缓存最多挡 TTL 那么久。

    这条不是在测性能，是在钉住"多 worker 时用户看到旧值的最长时间"。TTL 一旦被
    改成几十秒，单机单 worker 下没问题，加了 worker 之后就会出现"保存了但页面还是
    旧值，刷新两次才好"的那种报告。
    """
    assert store._CACHE_TTL_SECONDS <= 1.0

    store.save(base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat")
    assert store.resolve().model == "deepseek-chat"

    # 模拟"另一个进程"：直接改库，绕过本进程的 save()（因此不会 _invalidate）。
    with closing(repository.connect()) as conn, conn:
        conn.execute(f"UPDATE {store.TABLE} SET model = 'other-worker-model' WHERE id = 1")

    # 缓存还在：这一步读到的仍是旧值——这正是 TTL 兜的那个窗口。
    assert store.resolve().model == "deepseek-chat"

    # 把时钟推过 TTL（不真 sleep，避免用例变慢也避免偶发）。
    key = store._cache_key()
    stamp, ready = store._CACHE[key]
    store._CACHE[key] = (stamp - store._CACHE_TTL_SECONDS - 0.01, ready)
    assert store.resolve().model == "other-worker-model"


def test_single_row_is_enforced_by_the_schema():
    """多行并存 = "到底哪一行生效"要靠 `ORDER BY` 猜。约束钉在 DDL 里。"""
    import sqlite3

    with closing(repository.connect()) as conn, conn:
        conn.execute(f"INSERT INTO {store.TABLE} (id, base_url) VALUES (1, 'https://a/v1')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO {store.TABLE} (id, base_url) VALUES (2, 'https://b/v1')")


def test_saved_row_is_readable_as_json_shape():
    """整行读出来就是响应要的形状（字段名对得上，不是一个内嵌对象）。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat",
        deep_model="deepseek-reasoner"))
    body = client.get("/api/settings/model").json()
    assert set(body) == {
        "base_url", "base_url_source", "api_key_masked", "api_key_source", "api_key_set",
        "model", "model_source", "deep_model", "deep_model_source",
        "deep_model_effective", "llm_mode", "configured", "updated_at",
    }
    # 打码值能被 JSON 序列化（省略号是普通字符，不是控制字符）。
    json.dumps(body, ensure_ascii=False)
