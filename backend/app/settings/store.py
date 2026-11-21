"""模型配置的落库与回落：一张单行表，一条优先级，一个打码函数。

**为什么要有这一层**：原来"用哪个模型服务"只存在于后端进程的环境变量里
（`MINDMAP_LLM_BASE_URL` / `_API_KEY` / `_MODEL`）。换一把密钥就要登服务器改
`EnvironmentFile` 再重启 systemd——而重启会把正在跑的长图打断。用户看得到的
那个页面里没有任何入口。

**为什么是单行表**：这套配置描述的是"这台机器用哪个模型服务"，不是"这个人偏好
什么"。仓库里没有登录/JWT（只有 nginx 的一层 Basic Auth，全局一个账号），所以
配置表**没有 `user_id` 维度**——加一列只会在换部署时变成一个永远为 NULL 的字段。
将来真要支持多用户，正确做法是给这张表加 `user_id` 并把 `id` 换成复合主键，
而不是现在先挖一个空维度。单行由 `CHECK (id = 1)` 在数据库层钉住：多行并存就
等于"到底哪一行生效"要靠 `ORDER BY` 猜。

**回落顺序（唯一一份实现，在 `resolve()` 里）**::

    库里的值（面板保存过）  →  环境变量  →  空（= 未配置）

**逐字段回落，不是整组回落**。只填了密钥、端点仍用环境变量，是这套配置最常见
的用法（换 key 比换供应商频繁得多）；按"整组"回落的话用户会被迫在面板里重填
一遍端点与模型名，而重填就会写错。

**密钥只出打码值**：`GET` 永远回 `…abcd`（末 4 位），明文只在进程内被
`resolve()` 与"测试连接"用到。这不是防住有服务器权限的人（他们能读 SQLite 文件），
而是防住三类真实泄漏：截图/录屏里带着整串 key、浏览器 devtools 的 Network 面板、
以及出错时被拼进某条日志或错误文案。

**明文落库**：`api_key` 在 SQLite 里是明文（与它躺在 `EnvironmentFile` 里是同一个
信任边界）。刻意不做"用一把写在代码里的密钥加密"——那只是把密钥换个地方放，
读代码的人一样能解开，却会让"忘了 `MINDMAP_DB_PATH` 指向哪"变成一种新的故障。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from app.core.errors import AppError

logger = logging.getLogger(__name__)

#: 表名与唯一那一行的 id。`CHECK (id = 1)` 与这里的常量是同一个决定的两半。
TABLE = "model_settings"
ROW_ID = 1

#: 环境变量名。与 `generation/service.py` 里的读法保持同一组名字——回落就是
#: "库里没有才发现它在这里"，名字一旦对不上，回落会静默变成"未配置"。
BASE_URL_ENV = "MINDMAP_LLM_BASE_URL"
API_KEY_ENV = "MINDMAP_LLM_API_KEY"
MODEL_ENV = "MINDMAP_LLM_MODEL"
#: 「深度思考」用的模型名。档 1 是这里的值（库 → 环境变量），档 2/3 的启发式在
#: `generation.service._model_for()` 里——那三档的先后只该有一份实现。
DEEP_MODEL_ENV = "MINDMAP_LLM_MODEL_DEEP"
#: 只读展示用：`demo` 时生成侧与视觉侧都不调模型。本模块**不改**它（那是部署
#: 决定），只是把它报给面板，好让"面板里配好了但生成不生效"有据可查。
LLM_MODE_ENV = "MINDMAP_LLM_MODE"

#: 三个域 + 一个派生域。`saved` = 面板保存过；`env` = 环境变量；
#: `unset` = 都没有；`fallback` 只属于 `deep_model`（启发式推出来的名字）。
SOURCE_SAVED = "saved"
SOURCE_ENV = "env"
SOURCE_UNSET = "unset"
SOURCE_FALLBACK = "fallback"

#: 长度上限。base_url / model 各 200，密钥 500：它们进 SQLite、进日志、进
#: 界面输入框，超长的值只会在排查时变成噪音（也顺手挡住"把整份证书贴进来"）。
MAX_URL_CHARS = 200
MAX_MODEL_CHARS = 200
MAX_KEY_CHARS = 500

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` 完全一致。三处格式不同会在
#: "谁更晚"上错一天（`app/agent/runs.py:_NOW` 写过这条教训）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_CREATE = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    base_url   TEXT NOT NULL DEFAULT '',
    api_key    TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    deep_model TEXT NOT NULL DEFAULT '',
    updated_at TEXT
)"""

#: 降级只告警一次。库坏了（文件被删、权限变了、盘满）时每一次模型调用都会走到
#: 这里，逐次 `warning` 会把日志刷满，反而看不出"从哪一刻开始坏的"。
_DEGRADED_WARNED = False


class SettingsError(AppError):
    """设置写入被拒。带着业务 code 出去，界面才有话可说。"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表。由 `repository.connect()` 在同一个连接上调用，幂等。

    与 `user_prefs` / `memory_items` / `library_sources` 建在同一时刻的理由一样：
    读它的路径（**每一次模型调用**，见 `generation.service._config`）在热路径上，
    多一个"发现表不存在 → 建表"的分支只会在最不该出错的地方多一条可能出错的路径。

    `_ensure_columns` 这一步今天是空操作（建表语句里已经有全部列），留着是为了
    下一个人往这张表加列时不必记得"`CREATE TABLE IF NOT EXISTS` 不会给**已经存在**
    的表补列"——线上那张表一旦有行，加列就再也进不去了，而缺列的表现是
    `SELECT` 抛 `no such column`，被下面的降级分支吞成"未配置"，看起来像"用户没设过"。
    """
    conn.execute(_CREATE)
    from app.mindmaps.repository import _ensure_columns

    _ensure_columns(conn, TABLE, {"deep_model": "TEXT", "updated_at": "TEXT"})


@dataclass(frozen=True)
class Ready:
    """库里那一行的原样值（**含明文密钥**，只在进程内流转，绝不进响应）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    deep_model: str = ""
    updated_at: str | None = None


@dataclass(frozen=True)
class Resolved:
    """一次解析的结果：四个生效值 + 每个值来自哪里。

    `*_source` 不是调试信息，它是界面上的「来自环境变量 / 你在面板里设的」标签：
    没有它，用户分不清"我改了但没生效"与"我改的本来就被环境变量盖住了"——而
    优先级是库 > 环境变量，所以"盖住"这件事在当前口径下不会发生，能发生的是
    "忘了保存"与"存了但连不上"，两者要靠来源标签 + 测试连接分开。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    deep_model: str = ""
    base_url_source: str = SOURCE_UNSET
    api_key_source: str = SOURCE_UNSET
    model_source: str = SOURCE_UNSET
    deep_model_source: str = SOURCE_UNSET


def _connect() -> sqlite3.Connection:
    # 函数内 import：`repository.connect()` 里要用本模块的 `ensure_schema`，模块顶层
    # 反向 import 回去就成环。与 `app/prefs/style.py` / `app/memory/store.py` 同一手法。
    from app.mindmaps import repository

    return repository.connect()


def _norm_base(text: str | None) -> str:
    """端点：去空白 + 去掉**末尾**斜杠。

    与 `generation.service._config()` 原来的处理逐字节一致（`strip().rstrip("/")`）。
    不规范化的话，`https://x/v1/` 会让请求打到 `https://x/v1//chat/completions`——
    多数兼容端点容忍，少数直接 404，而 404 在界面上只能读成"模型名不对"。
    """
    return (text or "").strip().rstrip("/")


def _norm_text(text: str | None) -> str:
    return (text or "").strip()


def saved() -> Ready:
    """库里那一行。没有行、或者库读不出来，都返回全空。

    **读失败时降级而不是抛**：这条路径在每一次模型调用的开头，让它因为一个读不到
    的配置表把整次生成打成失败，比"回落到环境变量"糟糕得多——后者在绝大多数部署
    上恰好是正确的行为（环境变量本来就配好了）。降级会留一条 warning，不静默。
    """
    global _DEGRADED_WARNED
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                f"SELECT base_url, api_key, model, deep_model, updated_at "
                f"FROM {TABLE} WHERE id = ?", (ROW_ID,)).fetchone()
    except sqlite3.Error as exc:
        if not _DEGRADED_WARNED:
            _DEGRADED_WARNED = True
            logger.warning(
                "读不到 %s 表（%s），模型配置整体回落到环境变量；后续同类错误不再重复告警",
                TABLE, type(exc).__name__)
        return Ready()
    if row is None:
        return Ready()
    return Ready(
        base_url=row["base_url"] or "",
        api_key=row["api_key"] or "",
        model=row["model"] or "",
        deep_model=row["deep_model"] or "",
        updated_at=row["updated_at"],
    )


def _pick(stored: str, env_name: str, *, normalize) -> tuple[str, str]:
    """一个字段的回落：库 → 环境变量 → 空。

    **逐字段**（见文件头）：库里有值就用库里的，没有才看环境变量。反过来
    （环境变量优先）会让"用户在面板里改完立刻生效"变成一个谎。
    """
    value = normalize(stored)
    if value:
        return value, SOURCE_SAVED
    value = normalize(os.getenv(env_name, ""))
    if value:
        return value, SOURCE_ENV
    return "", SOURCE_UNSET


def resolve() -> Resolved:
    """生效的模型配置。**全仓唯一一处定义"库 > 环境变量"的地方**。

    调用方：`generation.service._config()`（生成与工具两条出口）、
    `ingestion.vision_pdf._config()`（视觉提取）、`settings.router`（面板与测试连接）。
    三处都从这里取，口径才不会分叉——AGENTS.md 那条"改一个能力的入口，要检查所有
    会走到它的路径"在这里的落法就是：**别的出口不许自己 `os.getenv` 这三个名字**。
    """
    stored = saved()
    base_url, base_url_source = _pick(stored.base_url, BASE_URL_ENV, normalize=_norm_base)
    api_key, api_key_source = _pick(stored.api_key, API_KEY_ENV, normalize=_norm_text)
    model, model_source = _pick(stored.model, MODEL_ENV, normalize=_norm_text)
    deep_model, deep_model_source = _pick(
        stored.deep_model, DEEP_MODEL_ENV, normalize=_norm_text)
    return Resolved(
        base_url=base_url,
        api_key=api_key,
        model=model,
        deep_model=deep_model,
        base_url_source=base_url_source,
        api_key_source=api_key_source,
        model_source=model_source,
        deep_model_source=deep_model_source,
    )


def deep_override() -> tuple[str, str]:
    """「深度思考」显式配置的模型名，以及它来自哪里。空串 = 没配。

    单独一个函数而不是让 `_model_for` 直接读环境变量：那一档的优先级（库 → 环境
    变量）必须与其余三个字段同源，否则"面板里填了深度模型"会对工具内部那几次
    调用无效，而界面上 `done.model` 显示的是外层那一次——又是"看起来对了，只有
    产物是错的"。
    """
    stored = saved()
    return _pick(stored.deep_model, DEEP_MODEL_ENV, normalize=_norm_text)


def mask_api_key(key: str) -> str:
    """密钥打码：**只露末 4 位**，前缀统一是一个省略号。

    三条规则，都是从真实泄漏面倒推的：

      - 空 → 空串。界面据此显示"未配置"，而不是一个看起来像配置了的 `…`；
      - 长度 ≤ 8 → 只给 `…`。短 key（自建端点的口令常常是 8 位以内）露末 4 位
        等于把一半以上送出去，而它本来就不该指望"打码之后还认得出来"；
      - 其余 → `…` + 末 4 位。

    不保留 `sk-` 之类的前缀：那只是供应商的命名习惯，露出来对用户没有信息量，
    对截图里的旁观者倒是多一段可辨识的串。
    """
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "…"
    return "…" + text[-4:]


def llm_mode() -> str:
    """当前 `MINDMAP_LLM_MODE` 的原值（小写）。**只读**，供面板提示用。

    刻意不参与"配好没配好"的判定：`demo` 是部署方定的，配置面板改不动它。
    它出现在响应里是因为有一个真实的坑——`.env.example` 的默认值就是 `demo`，
    本机照抄之后即使在面板里填好了模型，agent 那条路仍然产出占位图。
    """
    return os.getenv(LLM_MODE_ENV, "").strip().lower()


def _check_base_url(text: str) -> str:
    """端点只收 http(s)。空串合法，意思是"清掉库里的值、回落到环境变量"。"""
    value = _norm_base(text)
    if not value:
        return ""
    if len(value) > MAX_URL_CHARS:
        raise SettingsError(
            "settings_base_url_invalid",
            f"模型地址最多 {MAX_URL_CHARS} 个字符。", status=400)
    if not value.startswith(("https://", "http://")):
        # 与 `_config()` 的校验同一句话：配置面板里写错一个协议头，用户看到的
        # 必须是"地址要 http(s) 开头"，而不是等到生成时报"模型服务不可用"。
        raise SettingsError(
            "settings_base_url_invalid",
            "模型地址要以 https:// 或 http:// 开头，例如 https://api.deepseek.com/v1。",
            status=400)
    return value


def _check_text(value: str, *, label: str, limit: int) -> str:
    text = _norm_text(value)
    if len(text) > limit:
        raise SettingsError(
            "settings_value_too_long", f"{label}最多 {limit} 个字符。", status=400)
    return text


def save(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    deep_model: str | None = None,
    clear_api_key: bool = False,
) -> tuple[Resolved, bool]:
    """写配置，返回（写入后的生效配置，这次有没有真的改变）。

    **三个字段的"空串"含义不同，是有意为之**：

      - `api_key=None / ""` → **不改**。界面上那个框显示的是打码值（`…abcd`），
        用户不动它时提交的就是空串；把空串当"清空密钥"，等于每次改个模型名就
        把密钥抹掉一次。要清空必须显式 `clear_api_key=True`。
      - `base_url` / `model` / `deep_model` 的 `None` → 不改；`""` → **清掉库里的
        值，回落到环境变量**。这三个不是密钥，界面上显示的是生效值本身，空的输入
        框就是"我不要这个覆盖了"；而"回到环境变量那套配置"是真实需求（换部署、
        回滚、临时排查）。

    `changed=False` 时**不刷 `updated_at`**：同值重复保存把时间戳刷成"刚刚"，
    界面上就会出现一条看起来刚改过、其实一直没变的记录——与 `user_prefs.set`
    同一条理由。
    """
    if clear_api_key and (api_key or "").strip():
        # 两个字段同时给是矛盾的：一个说"清掉"，一个说"设成这个"。静默让其中一个
        # 赢，下一次就会有人在这里丢掉一把刚填好的密钥。宁可 400。
        raise SettingsError(
            "settings_clear_conflict",
            "不能同时提交新密钥和「清除密钥」；请只留一个。", status=400)

    current = saved()
    next_base = current.base_url if base_url is None else _check_base_url(base_url)
    if clear_api_key:
        next_key = ""
    elif not (api_key or "").strip():
        # `None` 与空串是同一件事：**不改**。界面上那个框显示打码值（`…abcd`），
        # 用户不动它时提交的就是空串；把空串当"清空密钥"，等于每次改个模型名就把
        # 密钥抹掉一次。要清空只有 `clear_api_key=True` 一条路。
        next_key = current.api_key
    else:
        next_key = _check_text(api_key, label="密钥", limit=MAX_KEY_CHARS)
    next_model = (current.model if model is None
                  else _check_text(model, label="模型名", limit=MAX_MODEL_CHARS))
    next_deep = (current.deep_model if deep_model is None
                 else _check_text(deep_model, label="深度思考模型名", limit=MAX_MODEL_CHARS))

    changed = (next_base, next_key, next_model, next_deep) != (
        current.base_url, current.api_key, current.model, current.deep_model)
    if changed:
        with closing(_connect()) as conn, conn:
            conn.execute(
                f"""INSERT INTO {TABLE}
                        (id, base_url, api_key, model, deep_model, updated_at)
                    VALUES (?, ?, ?, ?, ?, {_NOW})
                    ON CONFLICT(id) DO UPDATE SET
                        base_url   = excluded.base_url,
                        api_key    = excluded.api_key,
                        model      = excluded.model,
                        deep_model = excluded.deep_model,
                        updated_at = excluded.updated_at""",
                (ROW_ID, next_base, next_key, next_model, next_deep),
            )
    return resolve(), changed


def clear() -> int:
    """清空配置行（只在用例与本地调试用）。返回删掉的行数。"""
    global _DEGRADED_WARNED
    _DEGRADED_WARNED = False
    with closing(_connect()) as conn, conn:
        return conn.execute(f"DELETE FROM {TABLE}").rowcount


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEEP_MODEL_ENV",
    "LLM_MODE_ENV",
    "MODEL_ENV",
    "Ready",
    "Resolved",
    "SOURCE_ENV",
    "SOURCE_FALLBACK",
    "SOURCE_SAVED",
    "SOURCE_UNSET",
    "SettingsError",
    "TABLE",
    "clear",
    "deep_override",
    "ensure_schema",
    "llm_mode",
    "mask_api_key",
    "resolve",
    "save",
    "saved",
]
