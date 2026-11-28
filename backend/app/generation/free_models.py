"""免费的模型档：动态发现，不写死名字。

Command Code 的 coding plan 里，标 `:free` 的那些模型在活动期间不收费；OpenCode
的目录里，`cost` 为 0 的聊天模型不收费。两个平台的**免费名单都不是常驻的**——会
新上架，也会下架。所以这里一个模型名都不硬编码：每次刷新都去 `GET {base}/models`
现问，筛出当前不收费的那批。

三条实测约束决定了这个模块的形状（`docs/FREE-MODELS.md` 有完整数据）：

1. **列表要 20 秒以上**（本机对 Command Code 两次实测 22.1s / 21.7s）。它绝不能
   出现在请求路径上：一次生成有 5–10 次模型调用，每次都等 22 秒等于把功能做成
   不可用。所以取列表是**后台线程 + TTL 缓存**，过期的旧列表继续用，刷新成功了
   再换。
2. **"免费"在两个目录里的写法不一样，但都只用目录自带的字段判断**：
   - Command Code：id 以 `:free` 结尾，且 `supported_endpoints` 里有
     `/chat/completions`。而且 id 必须带 `owner/` 前缀（`recursive/flint-v1.0:free`），
     文档里的短名直接 400 `unsupported_model`——所以只能原样用列表里的 id。
   - OpenCode（实测 `https://opencode.ai/inference/openai/v1/models`，带 key 返回
     200）：`type == "chat"` 且 `cost.text.input/output.price` 都是 0，且
     `recommended_api`（有就认）是 `chat-completions`。它的目录还带
     `capabilities.reasoning`（`effort` 档位列表 / `toggle`）——**思考档位因此是
     每模型各自的**，从目录读，不再写死一个 `"low"`（见 `FreeModel.effort`）。
3. **上游会偶发 429**（Command Code："Upstream model provider is temporarily
   unavailable"），同一时刻同一 key 连发三次也可能全 200。所以选取要能给出**候选
   序列**，让调用方在一次运行内逐个试，而不是撞上一个就整次失败。

**降级要留下痕迹**（AGENTS.md 那条）：拉不到列表时保留旧列表并记 warning，而不是
静默返回空——"没有免费档可用"和"这次没拉到列表"是两件事，前者用户该看到提示，
后者该让我们在日志里发现。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

# Command Code 那一侧的免费标识。**用 `endswith` 而不是 `in`**：模型名里带 `:free`
# 中段的那些不存在，但用 `in` 判断会顺手把将来可能出现的 `x:free-trial` 之类当免费，
# 而那类名字恰恰可能是要计费的档位。OpenCode 那一侧不用后缀判断（`big-pickle` 连
# `-free` 后缀都没有，照样免费），它的判据是 `cost`。
FREE_SUFFIX = ":free"

# 总开关。默认关，因为打开它就等于改变"这一次生成花谁的钱"：已有的部署把密钥放在
# 环境变量里、跑的是付费模型，升级之后如果默认改走免费档，行为会静默变成又慢又受
# 每日次数限制。**关掉时必须与上一个提交逐字节一致**（AGENTS.md）。
ENABLED_ENV = "MINDMAP_FREE_TIER"

# 列表的存活时间。取 1 小时是因为上下架不是分钟级事件，而每次刷新要占上游 20 秒；
# 比这更短只会增加上游压力，更长则让新上架的免费模型一整天用不上。
TTL_SECONDS = 60 * 60.0

# 拉列表的超时。它比实测的 22 秒留出余量，但不能无限等：刷新线程挂着不结束，
# 下一轮就永远不会开始，缓存会停在一个更旧的时间点上。
TIMEOUT_SECONDS = 45.0

# 只看这几个字段，别的字段即使上游加了也不影响我们。
_MODELS_PATH = "/models"

# 思考档位从"最省"到"最费"的全序。目录给的 `effort` 列表里有 `none` 就发 `none`
#（实测目录上 nemotron-super / hy4 直接列了它，deepseek 靠 `toggle` 加出来）；
# 没有就取列表里**最省的那一档**——免费模型把 token 烧在思考上是实测过的失败
# 形状（正文一个字没写就 `finish_reason=length`），"最省"正是冲着它去的。
_EFFORT_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}

# 档位的合法值域（排名顺序）。设置面板把"思考强度"存进用户的配置行时按它校验：
# 发一个目录没声明的档位是实测会 400 的形状，而这份七词集合是各家 effort 选项的
# 并集——面板的选项可以比它窄（目录说了才算），但用户存下来的值必须落在它里面。
EFFORT_WORDS = tuple(_EFFORT_RANK)

# 目录里**没有**思考档位信息时的兜底。Command Code 的 `/models` 就是这种形状
#（没有 `capabilities`）：它那两个免费模型实测拒绝 `off`、接受 `low`，而 `low`
# 是实测能把 reasoning 压下去的最低可用档（8.9 秒对 56.9 秒）。这份知识是那家
# 供应商的事实，放在"认得出这种形状"的这个模块里，别处不引用。
FALLBACK_EFFORT = "low"


@dataclass(frozen=True)
class FreeModel:
    """一个免费模型的全部事实：名字、思考档位、上下文窗口。

    `effort` 是**这一个模型**该发的 `reasoning_effort`，不是全局配置：

      - `"none"` —— 目录说它可以把思考关掉（effort 列表里有，或 `toggle` 为真）；
      - 其它字符串 —— 目录给了档位列表但没有 `none`，取列表里最省的那档；
      - `None` —— 目录没给档位信息（Command Code 的形状，或 OpenCode 上那四个
        无档位模型：big-pickle / iceman / lightning / mimo）。**此时请求里不发
        `reasoning_effort` 这个键**——Command Code 侧由 `FALLBACK_EFFORT` 兜住，
        OpenCode 侧发一个它没声明的档位才是真的会 400。

    `context` 只参与排序（窗口大的先用），对调用方没有别的意义。

    `effort_options` 是这一个模型在目录里声明的**全部档位**（目录顺序；`toggle`
    为真时把 `none` 前置）——设置面板的"思考强度"下拉用它。空元组 = 目录没给
    档位信息（Command Code 的形状），面板据此不显示下拉。它必须完整：面板里
    用户挑哪档我们就发哪档，不是只有"最省"那一档说了算。
    """

    id: str
    effort: str | None
    context: int
    effort_options: tuple[str, ...] = ()


# 进程内缓存。按 base_url 分：不同的人可能配了不同的端点，而"有哪些免费模型"
# 是那个端点的事实，不是全局事实。
#   base → (取到列表的时刻, 免费模型列表[已排序])
_CACHE: dict[str, tuple[float, list[FreeModel]]] = {}
# 正在刷新的 base。防重入：一小时内只会有一个线程去问上游，其余的立刻拿到旧列表。
_REFRESHING: set[str] = set()
_LOCK = threading.Lock()


def enabled() -> bool:
    """免费档是否被打开。**每次现读环境变量**，不在 import 时定死：部署方改完
    环境变量重启进程才生效是对的，但测试要能 monkeypatch 它。"""
    return os.getenv(ENABLED_ENV, "").strip() == "1"


def reset() -> None:
    """清空缓存。只给用例与"手工强制刷新"用，正常路径不该调它。"""
    with _LOCK:
        _CACHE.clear()
        _REFRESHING.clear()


def _context_of(model: dict[str, Any]) -> int:
    """两个目录各叫一个名字：Command Code 是顶层 `context_length`，OpenCode 塞在
    `limit.context` 里。取不到或不是正整数按 0 处理（排最后，不参与比较）。"""
    length = model.get("context_length")
    if not (isinstance(length, int) and length > 0):
        limit = model.get("limit")
        length = limit.get("context") if isinstance(limit, dict) else None
    return length if isinstance(length, int) and length > 0 else 0


def _from_commandcode(model: dict[str, Any], model_id: str) -> FreeModel | None:
    """Command Code 形状：`:free` 后缀 + `supported_endpoints` 里有
    `/chat/completions`。目录不带思考档位，按 `FALLBACK_EFFORT` 发。"""
    if not model_id.endswith(FREE_SUFFIX):
        # 用 endswith 而不是 "free" in model_id：`x:free-trial` 可能是要计费的档位。
        return None
    endpoints = model.get("supported_endpoints")
    if not isinstance(endpoints, list) or "/chat/completions" not in endpoints:
        return None
    return FreeModel(model_id, FALLBACK_EFFORT, _context_of(model))


def _from_opencode(model: dict[str, Any], model_id: str) -> FreeModel | None:
    """OpenCode 形状：`type == "chat"`、`cost.text` 两个 price 都是 0、
    `recommended_api`（字段存在时）是 `chat-completions`。思考档位从
    `capabilities.reasoning` 里读，`toggle` 为真视同列表里有 `none`。"""
    if model.get("type") != "chat":
        return None
    cost = model.get("cost")
    if not isinstance(cost, dict):
        return None
    prices = cost.get("text")
    if not isinstance(prices, dict):
        return None
    input_price = (prices.get("input") or {}).get("price")
    output_price = (prices.get("output") or {}).get("price")
    if input_price != 0 or output_price != 0:
        return None
    recommended = model.get("recommended_api")
    if recommended is not None and recommended != "chat-completions":
        return None
    capabilities = model.get("capabilities")
    reasoning = capabilities.get("reasoning") if isinstance(capabilities, dict) else None
    efforts = reasoning.get("effort") if isinstance(reasoning, dict) else None
    ranked = (
        [value for value in efforts if isinstance(value, str) and value in _EFFORT_RANK]
        if isinstance(efforts, list) else []
    )
    toggle = isinstance(reasoning, dict) and reasoning.get("toggle") is True
    if "none" in ranked or toggle:
        effort: str | None = "none"
    elif ranked:
        effort = min(ranked, key=_EFFORT_RANK.__getitem__)
    else:
        effort = None
    # 下拉的完整选项：目录给的顺序（上游把哪个放前面是它对"推荐程度"的表态），
    # `toggle` 加出来的 none 放最前面——用户读起来"关掉"排第一最直观。
    options: tuple[str, ...] = tuple(ranked)
    if toggle:
        options = ("none",) + tuple(word for word in options if word != "none")
    return FreeModel(model_id, effort, _context_of(model), options)


def pick_free(data: Iterable[Any]) -> list[FreeModel]:
    """从 `/models` 的 `data` 数组里挑出当前可用的免费模型，**已排好序**。

    纯函数，不碰网络也不读缓存——这是本模块唯一值得单测的部分。

    形状识别靠字段签名，不靠顺序或猜测：带 `supported_endpoints` 的按 Command
    Code 的规矩判（后缀 + 端点白名单），带 `type`/`cost` 的按 OpenCode 的规矩判
    （type + price 为 0）。两个都不像的条目直接跳过——**字段缺失时按"不支持"
    处理**，宁可少一个候选，也不要在运行时才发现它调不通。

    排序是"最该先用哪个"：**上下文大的优先，同大按 id 升序**。上下文优先是因为
    免费档的常见用途是"让没有密钥的人也能看到一张完整的图"，窗口越大越不容易在
    中途撞上限（那会变成一次跑到一半才失败的生成）。id 升序是**确定性**：上游
    返回顺序换个版本就可能变，两个模型同时上架的顺序也不该影响"今天用哪个"。
    """
    chosen: list[FreeModel] = []
    for model in data:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if isinstance(model.get("supported_endpoints"), list):
            record = _from_commandcode(model, model_id)
        elif "type" in model or "cost" in model:
            record = _from_opencode(model, model_id)
        else:
            record = None
        if record is not None:
            chosen.append(record)
    chosen.sort(key=lambda record: (-record.context, record.id))
    return chosen


def _fetch(base_url: str) -> list[FreeModel]:
    """问一次上游，返回排好序的免费模型。

    抛异常由调用方决定怎么处理（`_refresh` 里统一吞掉并记日志）。

    **这里必须绕过 `_config()`**：它走 `settings.store.resolve()`，而 `resolve()`
    正是本模块的调用方。用它会变成 `resolve() → free_models() → _fetch() →
    _config() → resolve()` 的无限递归，而且因为缓存机制它不会立刻炸，而是在
    "第一次需要刷新"的那一刻才栈溢出——最难查的那种。

    所以取地址与密钥走 `store.free_tier_endpoint()`：它只回答"部署方那台端点
    和它的 key 是什么"，不做任何免费档替换。

    **带鉴权**：两个平台的 `/models` 此刻都不要 key 也返回列表（Command Code
    实测无 token 与带 token 的 105 个 id 完全一致；OpenCode 的 zen 目录同样开放），
    但带上没有代价，而且将来它要是变成需要权限的接口（OpenCode 的推理端点
    `/inference/openai/v1/models` 实测带 key 才返回完整字段），不带就会静默退化
    成"永远没有免费档"——那是最难查的一种坏法。
    """
    from app.settings import store as settings_store

    base, key = settings_store.free_tier_endpoint()
    base = base or base_url
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = httpx.get(
        base.rstrip("/") + _MODELS_PATH,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("模型列表的形状不对：没有 data 数组")
    return pick_free(data)


def _refresh(base_url: str) -> None:
    """刷新一个 base 的免费模型列表。**永不抛异常**——它在后台线程里跑，
    抛出去只会变成一条没人接的线程异常栈。"""
    try:
        models = _fetch(base_url)
    except Exception as exc:  # noqa: BLE001 — 后台线程，任何异常都只能记下来
        with _LOCK:
            stale = _CACHE.get(base_url)
            # **失败也要放掉这个标记**。只放成功那一侧的话，一次失败会让这个 base
            # 一小时内再也不会重试（`_start_refresh` 看到它在集合里就直接返回），
            # 表现是"免费档时好时坏、重启之后又好了"——最像玄学的那种。
            _REFRESHING.discard(base_url)
        if stale is None:
            logger.warning(
                "拉不到模型列表，暂按没有免费档处理：%s: %s", type(exc).__name__, exc)
        else:
            age = int((time.monotonic() - stale[0]) / 60)
            logger.warning(
                "刷新模型列表失败，继续用 %d 分钟前的列表（%d 个免费模型）：%s: %s",
                age, len(stale[1]), type(exc).__name__, exc)
        return
    with _LOCK:
        _CACHE[base_url] = (time.monotonic(), models)
        _REFRESHING.discard(base_url)
    if models:
        logger.info("免费模型列表已刷新：%s", ", ".join(record.id for record in models))
    else:
        # 这一条要能看见：免费活动结束之后，界面上"用免费档生成"会一直失败，
        # 而日志里没有任何"当前 0 个免费模型"的记录会让排查从"查日志"变成"猜"。
        logger.warning(
            "模型列表里当前没有任何免费可用的模型（`:free` 后缀或 cost=0）")


def _stale(base_url: str) -> bool:
    with _LOCK:
        entry = _CACHE.get(base_url)
    if entry is None:
        return True
    return (time.monotonic() - entry[0]) > TTL_SECONDS


def _start_refresh(base_url: str) -> None:
    """在后台线程里刷新，**本调用立即返回**。已经有了就直接跳过。"""
    with _LOCK:
        if base_url in _REFRESHING:
            return
        _REFRESHING.add(base_url)
    threading.Thread(
        target=_refresh, args=(base_url,),
        name="free-models-refresh", daemon=True,
    ).start()


def free_models(base_url: str) -> list[FreeModel]:
    """当前已知的免费模型，**按优先级排好序**。空列表 = 此刻没有免费档可用。

    语义是"现在能拿到的最好答案"：

      - 缓存新鲜 → 直接返回；
      - 缓存过期 → 起一个后台线程刷新，**这一秒仍然返回旧列表**；
      - 一次都没拉到过 → 返回空列表，同时起后台线程。

    第一次调用返回空是**有意**的：为了第一次请求能立刻出结果，宁可这一次报
    "免费档还没准备好"（用户点一下重试就好，后台已经拉好了），也不要让每个
    第一次访问的人都盯着"正在连接"等 22 秒。

    空列表与"拉取失败"在调用方看来是同一件事，这是可接受的：两者的正确反应都是
    "这次别用免费档"。
    """
    if not enabled():
        return []
    base = (base_url or "").rstrip("/")
    if not base:
        return []
    if _stale(base):
        _start_refresh(base)
    with _LOCK:
        entry = _CACHE.get(base)
    return list(entry[1]) if entry else []


def candidates(base_url: str, tried: Iterable[str] = ()) -> list[FreeModel]:
    """本次还能试的免费模型：全部候选里去掉已经试过的那些。

    调用方在一次运行内逐个试（上游 429 时换下一个），所以"试过哪个"必须由调用方
    记着——本模块不持有"这次运行"的状态：那是每请求的东西，放模块级会让并发请求
    互相污染（A 试过 flint，B 就永远拿不到 flint）。
    """
    done = {item for item in tried}
    return [record for record in free_models(base_url) if record.id not in done]


def current(base_url: str) -> FreeModel | None:
    """首选的那个免费模型，没有就是 None。"""
    found = free_models(base_url)
    return found[0] if found else None


def options_for(base_url: str, model_id: str) -> tuple[str, ...]:
    """这一个模型在目录里声明的思考档位（按目录顺序），没有就是空元组。

    给设置面板的"思考强度"下拉用。走 `free_models()` 的同一套缓存：列表没拉到过
    时返回空，面板据此显示"沿用服务商默认"而不是自己编一排档位——发一个目录没
    声明的档位是实测会 400 的形状。
    """
    for record in free_models(base_url):
        if record.id == model_id:
            return record.effort_options
    return ()


__all__ = [
    "EFFORT_WORDS",
    "ENABLED_ENV",
    "FREE_SUFFIX",
    "FALLBACK_EFFORT",
    "FreeModel",
    "TIMEOUT_SECONDS",
    "TTL_SECONDS",
    "candidates",
    "current",
    "enabled",
    "free_models",
    "options_for",
    "pick_free",
    "reset",
]
