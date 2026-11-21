"""模型设置的 HTTP 面：读当前生效配置、写一份新的、测一次连接。

三个动词就是面板的全部行为，一一对应：`GET` 填表、`PUT` 保存、`POST .../test` 验一次。

**这里没有"从环境变量导入"的入口**。库里的值与环境变量是两个来源，优先级写死在
`store.resolve()` 里；再加一个"把环境变量拷进库"的按钮，只会制造第三态——
"拷过了，之后改环境变量为什么不生效"。用户的动作只有一种：填、存、测。

**响应里没有任何明文密钥**。`api_key_masked` 是唯一会出门的密钥形状（`…abcd`），
连"长度"都不给：长度对撞库没有帮助，对截图里的旁观者倒是有。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.generation.service import _model_for
from app.settings import probe as probe_module
from app.settings import store

router = APIRouter(prefix="/settings", tags=["模型设置"])


class ModelSettings(BaseModel):
    """生效的模型配置。**密钥只有打码值**。"""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    #: `saved` / `env` / `unset`。界面据此标「你在面板里设的」或「来自环境变量」。
    base_url_source: str
    #: `…abcd`；没配时是空串。**永远不返回明文**。
    api_key_masked: str
    api_key_source: str
    #: 有没有密钥。界面用它决定"清除密钥"按钮可不可点。
    api_key_set: bool
    model: str
    model_source: str
    #: 显式配置的「深度思考」模型名，空串 = 没配（走 `_model_for` 的启发式）。
    deep_model: str
    deep_model_source: str
    #: 勾了「深度思考」时**实际会发出去**的那个名字。没配显式值时它由
    #: `_model_for` 的三档启发式算出（deepseek 端点 → `deepseek-reasoner`，
    #: 否则沿用原模型名）。面板显示它，用户才不会以为"深度思考没生效"。
    deep_model_effective: str
    #: `MINDMAP_LLM_MODE` 的原值（`demo` / `model` / 空）。面板用它提示"本机是
    #: 演示模式，配好了也不会调模型"。**只读**，面板改不动它。
    llm_mode: str
    #: 三项（地址 / 密钥 / 模型名）齐了没有。判据与 `_config()` 一致。
    configured: bool
    #: 库里那一行最后一次被保存的时刻；没保存过是 `null`。
    updated_at: str | None = None


def _view(resolved: store.Resolved, *, updated_at: str | None) -> ModelSettings:
    """`Resolved` → 响应体。**密钥在这里被换成打码值，这是唯一一处出口**。"""
    effective = _model_for(resolved.base_url, resolved.model, True) if resolved.model else ""
    return ModelSettings(
        base_url=resolved.base_url,
        base_url_source=resolved.base_url_source,
        api_key_masked=store.mask_api_key(resolved.api_key),
        api_key_source=resolved.api_key_source,
        api_key_set=bool(resolved.api_key),
        model=resolved.model,
        model_source=resolved.model_source,
        deep_model=resolved.deep_model,
        deep_model_source=resolved.deep_model_source,
        deep_model_effective=effective,
        llm_mode=store.llm_mode(),
        configured=_configured(resolved),
        updated_at=updated_at,
    )


def _configured(resolved: store.Resolved) -> bool:
    """与 `generation.service._config()` 同一个判据：地址要 http(s) 开头，三项都非空。"""
    return (resolved.base_url.startswith(("https://", "http://"))
            and bool(resolved.api_key) and bool(resolved.model))


@router.get("/model", response_model=ModelSettings)
def read_model_settings() -> ModelSettings:
    """当前生效的配置 + 每一项的来源。

    `changed` 之类的写入信息不在这里——`GET` 只回答"现在是什么"。
    """
    resolved = store.resolve()
    return _view(resolved, updated_at=store.saved().updated_at)


class SaveModelSettingsRequest(BaseModel):
    """`PUT /settings/model` 的请求体。**每个字段的"空"含义不同**（见 `store.save`）。

      - `api_key` 省略或空串 → **不改**（界面上那个框显示打码值，用户不动它就发空串）；
        要清空必须 `clear_api_key: true`。
      - `base_url` / `model` / `deep_model` 省略 → 不改；空串 → 清掉库里的覆盖，
        回落到环境变量。

    `extra="forbid"` 是仓库的默认风格：拼错字段名时宁可 422，也不要静默忽略——
    "我把密钥填进 `apikey` 了，保存成功但没生效"是最难自查的一类失败。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    deep_model: str | None = None
    #: 显式清除已经保存的密钥。与同时给 `api_key` 冲突（400）。
    clear_api_key: bool = False


class SaveModelSettingsResponse(ModelSettings):
    """保存的结果：写入后的生效配置 + 这次有没有真的改变。

    `changed=False` 是"这次提交没有改变任何东西"（同值重复保存不刷 `updated_at`）。
    不返回这个布尔值，界面上就会出现"我刚存过"的假象。
    """

    changed: bool


@router.put("/model", response_model=SaveModelSettingsResponse)
def save_model_settings(body: SaveModelSettingsRequest) -> SaveModelSettingsResponse:
    """保存。**改完立即生效**：所有出口每次调用都重新 `resolve()`，没有进程内缓存。

    `store.SettingsError`（`AppError` 子类）由 `core/errors.py` 的统一出口转成
    `{code, message, detail, request_id}`——错误码分得开，界面才能说清"是地址不合法
    还是密钥太长"。
    """
    resolved, changed = store.save(
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        deep_model=body.deep_model,
        clear_api_key=body.clear_api_key,
    )
    return SaveModelSettingsResponse(
        **_view(resolved, updated_at=store.saved().updated_at).model_dump(),
        changed=changed,
    )


class TestModelSettingsRequest(BaseModel):
    """`POST /settings/model/test` 的请求体：可选的临时覆盖。

    空字段 = 用**当前生效**的值。这样面板可以"先测再存"：用户在输入框里改了地址
    或模型名，不必先落库就能验一次。密钥同样：不填就用已保存/环境变量里那把，
    所以"我只改模型名"的场景不需要再手打一遍 key。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class TestModelSettingsResponse(BaseModel):
    """一次测试连接的结论。`ok=false` 也是 200——它是结论，不是 HTTP 失败。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    message: str
    #: 这次真正拿去测的模型名（可能在深度思考档位之外，就是生效值）。
    model: str
    status: int | None = None
    latency_ms: int = 0


@router.post("/model/test", response_model=TestModelSettingsResponse)
async def test_model_settings(
    body: TestModelSettingsRequest | None = None,
) -> TestModelSettingsResponse:
    """发一次最小 chat 请求，验证地址 / 密钥 / 模型名。

    **为什么是 200 而不是 4xx**：失败原因分成"地址不通""密钥被拒""模型不存在"
    三种，界面要按 `ok` 显示在面板里（一句红字），而不是让前端把 4xx 当成"请求
    没发出去"。真正的业务错误（比如三项都没配）走 400，因为那时连测都没法测。
    """
    overrides = body or TestModelSettingsRequest()
    resolved = store.resolve()
    base_url = (overrides.base_url or "").strip().rstrip("/") or resolved.base_url
    api_key = (overrides.api_key or "").strip() or resolved.api_key
    model = (overrides.model or "").strip() or resolved.model
    if not base_url or not api_key or not model:
        _raise_incomplete()
    if api_key in (store.mask_api_key(resolved.api_key), store.mask_api_key(store.saved().api_key)):
        # 提交的是打码值（`…abcd`）。真拿去发请求，上游会 401，而这里会把它翻译成
        # "密钥被拒"——用户于是去供应商后台换密钥，而他手里那把其实一直是好的。
        # 与 `save()` 里那条判据同一个理由：打码值不是密钥，任何一次都不能当密钥用。
        raise store.SettingsError(
            "settings_api_key_masked",
            "提交的是打码后的密钥（形如 …abcd），不是密钥本身；"
            "要测连接请重新粘贴完整的值，或者留空直接用已保存的那把。",
            status=400)
    result = await probe_module.probe(base_url, api_key, model)
    return TestModelSettingsResponse(
        ok=result.ok, message=result.message, model=result.model,
        status=result.status, latency_ms=result.latency_ms,
    )


def _raise_incomplete() -> None:
    """三项不齐时抛统一业务错误。抽出来只为让上面那段的判断读起来是一句话。"""
    raise store.SettingsError(
        "settings_incomplete",
        "地址、密钥、模型名三项要先填齐才能测试连接。", status=400)


__all__ = ["router"]
