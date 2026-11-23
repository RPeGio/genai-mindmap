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
import time
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import urlsplit

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
#: 库里有密钥，但它绑定的那台地址已经不是当前生效的地址了。**这时不交出密钥**。
#:
#: 它不是"没配"：面板要能说清"你换过地址，密钥需要重新填一次"，而 `unset` 那句话
#: （去配一个）会让用户以为密钥丢了。见 `resolve()` 里那段判定。
SOURCE_UNBOUND = "unbound"

#: 长度上限。base_url / model 各 200，密钥 500：它们进 SQLite、进日志、进
#: 界面输入框，超长的值只会在排查时变成噪音（也顺手挡住"把整份证书贴进来"）。
MAX_URL_CHARS = 200
MAX_MODEL_CHARS = 200
MAX_KEY_CHARS = 500

#: 生成侧与 probe 拼请求时一律用 `base + "/chat/completions"`，所以库里存的必须是
#: **前缀**。用户粘完整端点时由 `_check_base_url` 剥掉这一段。
_ENDPOINT_SUFFIX = "/chat/completions"

#: 写配置时等写锁的上限。这是面板里人手点的一个动作：让他看着转圈等十秒，不如
#: 立刻告诉他"正被占用，过几秒重试"（与 `agent/runs.py` 的检查点同一个理由）。
SETTINGS_LOCK_TIMEOUT = 2.0

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` 完全一致。三处格式不同会在
#: "谁更晚"上错一天（`app/agent/runs.py:_NOW` 写过这条教训）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_CREATE = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    base_url     TEXT NOT NULL DEFAULT '',
    api_key      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    deep_model   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT,
    api_key_host TEXT NOT NULL DEFAULT ''
)"""

#: 降级只告警一次。库坏了（文件被删、权限变了、盘满）时每一次模型调用都会走到
#: 这里，逐次 `warning` 会把日志刷满，反而看不出"从哪一刻开始坏的"。
_DEGRADED_WARNED = False

# ---------------------------------------------------------------- 读缓存
#
# **为什么需要**：`saved()` 在每一次模型调用的开头被 `resolve()` 调到（见
# `generation.service._config`）。实测一次生成里的调用次数：
#
#   - 整树生成（`mindmap_generate`，骨架 + 结构 + 说明 3 次工具内调用，加外层
#     provider 2 轮）= **5 次**；
#   - 分片生成（`mindmap_generate_deep`，4 支 × 3 点展开 = 25 次工具内调用，加
#     外层 2 轮）= **27 次**。
#
# 每次都要 `repository.connect()`，而那个函数开头有十条 `CREATE TABLE IF NOT
# EXISTS`——代价不在那条 `SELECT` 上，在这十条 DDL 上。27 次乘以十条 DDL 是每次
# 大图生成白付的开销，而这份配置在一次生成期间根本不会变。
#
# **为什么不是纯 TTL**：`save()` 之后必须立刻生效（这是这个功能的整个卖点）。所以
# 写入侧（`save` / `clear`）会 `_invalidate()`，**同进程内保存 → 下一次读到新值**。
# TTL（1 秒）只兜住另一种情况：多 worker 部署时另一个进程改了库，本进程看不到那次
# `_invalidate`。本仓的目标形态是单机单 worker，1 秒是给"以后有人加了 worker"留的
# 上限，不是给正常路径延迟的。
#
# 键里带 `db_path()`：测试每个用例换一个 `MINDMAP_DB_PATH`，不带路径的缓存会让
# 前一个用例的行漏到后一个用例里——那种"随机绿"最难查。
_CACHE_TTL_SECONDS = 1.0
_CACHE: dict[str, tuple[float, "Ready"]] = {}
#: 写入计数器。`save()` / `clear()` 加一，缓存项随之作废。用它而不是清空字典，
#: 是为了让"进程内写过的次数"本身成为一个可断言的事实（见测试）。
_WRITES = 0


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

    _ensure_columns(conn, TABLE, {
        "deep_model": "TEXT",
        "updated_at": "TEXT",
        # 密钥绑定在哪台主机上。老库补这一列时全部是空串 = "没绑定过"，
        # 那种行按"绑定校验不生效"处理（见 `resolve()`），不会在升级后
        # 突然把所有已存配置打成未配置。
        "api_key_host": "TEXT NOT NULL DEFAULT ''",
    })


@dataclass(frozen=True)
class Ready:
    """库里那一行的原样值（**含明文密钥**，只在进程内流转，绝不进响应）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    deep_model: str = ""
    updated_at: str | None = None
    #: 这把密钥当初是在哪台主机上填进去的。空串 = 没有记录（老行，或从没换过地址）。
    #: 它是"密钥不出门"那条防线的锚点：地址一变，锚点对不上就不交密钥。
    api_key_host: str = ""


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


def _connect(*, timeout: float | None = None) -> sqlite3.Connection:
    # 函数内 import：`repository.connect()` 里要用本模块的 `ensure_schema`，模块顶层
    # 反向 import 回去就成环。与 `app/prefs/style.py` / `app/memory/store.py` 同一手法。
    from app.mindmaps import repository

    if timeout is None:
        return repository.connect()
    return repository.connect(timeout=timeout)


def _norm_base(text: str | None) -> str:
    """端点：去空白 + 去掉**末尾**斜杠。

    与 `generation.service._config()` 原来的处理逐字节一致（`strip().rstrip("/")`）。
    不规范化的话，`https://x/v1/` 会让请求打到 `https://x/v1//chat/completions`——
    多数兼容端点容忍，少数直接 404，而 404 在界面上只能读成"模型名不对"。
    """
    return (text or "").strip().rstrip("/")


def _norm_text(text: str | None) -> str:
    return (text or "").strip()


def _host_of(base_url: str) -> str:
    """端点里的主机名（小写）。空串 = 地址为空或解析不出来。

    **只取主机名，不取端口、不取路径**。绑定要回答的问题是"这把密钥属于哪一台
    主机"，不是"属于哪个 URL"：同一台机器上 `/v1` 换成 `/v2`、Ollama 从 11434
    挪到 11435，密钥都还是同一把，为这些变动逼人重填只会训练出"随手再粘一次"的
    习惯——而那正是这道防线要避免的输入。反过来，换主机就意味着换了一个"会收到
    这把密钥的对方"，那才是要重新确认的事。

    解析失败（端口不是数字之类）返回空串：那种地址连 `_check_base_url` 都过不了，
    这里不必再抛一次。
    """
    try:
        return (urlsplit(_norm_base(base_url)).hostname or "").strip().lower()
    except ValueError:
        return ""


def _cache_key() -> str:
    """缓存键：库路径 + 写入计数。

    带路径的理由见 `_CACHE` 的注释（测试每个用例换一个库）。带写入计数的理由：
    `save()` 之后即使 TTL 还没到也必须作废，所以键里直接含"写过几次"——写一次，
    键就变了，旧项自然不再命中，不需要额外的失效逻辑去和并发读抢。
    """
    from app.mindmaps import repository

    return f"{repository.db_path()}::{_WRITES}"


def saved() -> Ready:
    """库里那一行。没有行、或者库读不出来，都返回全空。

    **读失败时降级而不是抛**：这条路径在每一次模型调用的开头，让它因为一个读不到
    的配置表把整次生成打成失败，比"回落到环境变量"糟糕得多——后者在绝大多数部署
    上恰好是正确的行为（环境变量本来就配好了）。降级会留一条 warning，不静默。

    **降级的结果不进缓存**：库坏掉是一个可能被修好的状态（盘满了、权限改回来），
    把"读不到"缓存一秒会让修好之后的第一秒继续走环境变量。代价是坏库时每次都真的
    去试一次——那本来就该被看见。
    """
    global _DEGRADED_WARNED
    key = _cache_key()
    cached = _CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                f"SELECT base_url, api_key, model, deep_model, updated_at, api_key_host "
                f"FROM {TABLE} WHERE id = ?", (ROW_ID,)).fetchone()
    except sqlite3.Error as exc:
        if not _DEGRADED_WARNED:
            _DEGRADED_WARNED = True
            logger.warning(
                "读不到 %s 表（%s），模型配置整体回落到环境变量；后续同类错误不再重复告警",
                TABLE, type(exc).__name__)
        return Ready()
    ready = Ready() if row is None else Ready(
        base_url=row["base_url"] or "",
        api_key=row["api_key"] or "",
        model=row["model"] or "",
        deep_model=row["deep_model"] or "",
        updated_at=row["updated_at"],
        api_key_host=_row_host(row),
    )
    _CACHE[key] = (time.monotonic(), ready)
    return ready


def _row_host(row: sqlite3.Row) -> str:
    """兼容"还没补上 `api_key_host` 列"的库。

    `_ensure_columns` 会让新库与新连接都带上这一列，但降级路径（`saved()` 读失败
    返回 `Ready()`）与"读到一半被别的进程改成老结构"这类边角不值得一次 500：
    缺列就当"没有绑定记录"，走的是与老行相同的"不校验"分支。用 `row.keys()`
    判而不是 try/except，是为了让 `IndexError` 真的冒出来——那是真 bug。
    """
    return (row["api_key_host"] or "") if "api_key_host" in row.keys() else ""


def _read_strict(*, timeout: float | None = None) -> Ready | None:
    """像 `saved()` 一样读那一行，但**读失败时抛、不降级**。只有写路径用。

    `saved()` 的降级是给**生成路径**准备的：读不到就回落环境变量，那在绝大多数
    部署上恰好是对的，绝不能因为一个读不出来的配置表把整次生成打成失败。

    写路径不能复用那个返回值。`save()` 拿它当"库里现在那一行"去做差量与合并
    （`next_base = current.base_url if base_url is None else ...`），降级返回的空
    `Ready()` 会被当成真值：只改一个模型名，`base_url` 与 `api_key` 就一起被写成
    空串，接口还回 `changed: true`。用户看到的是一次"保存成功"，丢掉的是密钥。

    降级读在写锁竞争下真会发生：`repository.connect(timeout=10)` 忙等十秒之后
    `saved()` 返回空 `Ready()` 并只打一条 warning，而那次写照样成功。
    """
    with closing(_connect(timeout=timeout)) as conn:
        row = conn.execute(
            f"SELECT base_url, api_key, model, deep_model, updated_at, api_key_host "
            f"FROM {TABLE} WHERE id = ?", (ROW_ID,)).fetchone()
    if row is None:
        return None
    return Ready(
        base_url=row["base_url"] or "",
        api_key=row["api_key"] or "",
        model=row["model"] or "",
        deep_model=row["deep_model"] or "",
        updated_at=row["updated_at"],
        api_key_host=_row_host(row),
    )


def _invalidate() -> None:
    """写完之后让缓存作废。由 `save()` / `clear()` 调用，别处不要碰。"""
    global _WRITES

    _WRITES += 1
    _CACHE.clear()


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


def _binding_of(stored: Ready, api_key_source: str) -> str:
    """这把密钥**被谁验证过**：能交出密钥的主机名。空串 = 没有锚点（见 `resolve()`）。

    两个来源，对应密钥的两个来处：

      - 面板里存的密钥 → `api_key_host` 列。老行（这一列还没有时写的）兜底用
        **那一行自己的 `base_url`**：同一行、同一次写入，地址与密钥本来就是一对。
        这个兜底只在读取时算，不写库——写库的时机是 `save()`，而 `save()` 在
        "地址变了但密钥没重填"时会把这个旧锚点**如实写下来**，于是敏感的那一步
        （换地址）立刻变成"锚点对不上"。
      - 环境变量里的密钥 → 同一组环境变量里那个 `MINDMAP_LLM_BASE_URL` 的主机名。
        部署方把两者写在一起，那一次写入就是它们的绑定动作。**只给了密钥没给地址**
        时无从校验（返回空串），这种情况下"谁配的地址谁说了算"，而那种部署本来
        就把密钥放在一台谁能改地址谁就能读环境变量的机器上。
    """
    if api_key_source == SOURCE_SAVED:
        return _norm_text(stored.api_key_host).lower() or _host_of(stored.base_url)
    if api_key_source == SOURCE_ENV:
        return _host_of(os.getenv(BASE_URL_ENV, ""))
    return ""


def resolve() -> Resolved:
    """生效的模型配置。**全仓唯一一处定义"库 > 环境变量"的地方**。

    调用方：`generation.service._config()`（生成与工具两条出口）、
    `ingestion.vision_pdf._config()`（视觉提取）、`settings.router`（面板与测试连接）。
    三处都从这里取，口径才不会分叉——AGENTS.md 那条"改一个能力的入口，要检查所有
    会走到它的路径"在这里的落法就是：**别的出口不许自己 `os.getenv` 这三个名字**。

    **密钥与地址是绑定的**（P0-1 的第二层）：这一函数是明文密钥的唯一出口，
    所以"这个地址配得上用这把钥匙吗"必须也在这里回答。地址的主机名与密钥的锚点
    不一致时，密钥**不交出去**，来源标记成 `unbound`——界面据此说"你换过地址，
    密钥需要重新填一次"，而不是悄悄把旧密钥发到新地址。

    为什么这才是那条攻击的真正止损点：`PUT /settings/model` 改地址本身不需要密钥，
    `POST /settings/model/test` 会带着**已存的**密钥去请求。绑定的判据不放在
    `probe()` 里，是因为发送点不止一个——生成、视觉提取、外部检索全都从这里取
    密钥，把判据放在出口上，所有路径一起受保护（放进入口就得记住每一个入口）。
    """
    stored = saved()
    base_url, base_url_source = _pick(stored.base_url, BASE_URL_ENV, normalize=_norm_base)
    api_key, api_key_source = _pick(stored.api_key, API_KEY_ENV, normalize=_norm_text)
    model, model_source = _pick(stored.model, MODEL_ENV, normalize=_norm_text)
    deep_model, deep_model_source = _pick(
        stored.deep_model, DEEP_MODEL_ENV, normalize=_norm_text)
    # 只在这个地址**真的存在**时校验：没有地址就无从"发错地方"，而那时
    # `configured` 已经是 False，界面上说的是另一个问题（去填地址）。
    # 锚点为空串 = 无从判断（环境里只给了密钥、没给地址），那时不拦——
    # 拦住它只会让这种部署升级后直接不可用，而它本来就把密钥交给了一台
    # "谁能改配置谁就能读环境变量"的机器。
    host = _host_of(base_url)
    if api_key and host:
        bound = _binding_of(stored, api_key_source)
        if bound and bound != host:
            api_key, api_key_source = "", SOURCE_UNBOUND
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
    # 用户常常从厂商文档里**连端点一起**复制过来（`https://x/v1/chat/completions`）。
    # 留着它请求会变成 `.../chat/completions/chat/completions`，404 在界面上只能
    # 读成"模型名不对"，于是人被引去改一个没问题的地方。入库前剥掉，probe 与
    # 生成两条路同时受益，面板回显的也是剥完的值。
    if value.endswith(_ENDPOINT_SUFFIX):
        value = value[: -len(_ENDPOINT_SUFFIX)].rstrip("/")
    if not value:
        # 只填了 `/chat/completions`：剥完是空串，那是"没填地址"，不是"清掉覆盖"。
        raise SettingsError(
            "settings_base_url_invalid",
            "模型地址只填了 /chat/completions，前面还差主机名，例如 https://api.deepseek.com/v1。",
            status=400)
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


def _read_strict_for_save() -> Ready:
    """`save()` 专用的读：**读不到就拒写**，绝不返回"空值"。

    与 `_read_strict()` 分开只是为了把"没有行"折成空 `Ready`——第一次保存时
    本来就没有行，那不是错误；而"读不出来"必须抛。
    """
    try:
        row = _read_strict(timeout=SETTINGS_LOCK_TIMEOUT)
    except sqlite3.Error as exc:
        # 宁可这次保存失败，也不能把"读不到"当成"是空的"：那会把用户已存的
        # base_url 与密钥一起写成空串，还回 200「保存成功」。
        raise SettingsError(
            "settings_db_unavailable",
            "读不到配置表，这次没有保存（继续下去会把你已存的值写成空）。请过几秒重试。",
            status=503) from exc
    return Ready() if row is None else row


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

    current = _read_strict_for_save()
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
        effective_key, _ = _pick(current.api_key, API_KEY_ENV, normalize=_norm_text)
        if next_key and next_key in (mask_api_key(current.api_key),
                                     mask_api_key(effective_key)):
            # 提交的是**打码后的样子**（`…abcd`），不是密钥本身。把它当新密钥存下去，
            # 库里那把真密钥就被这个字面量顶掉了：接口照旧回 `api_key_set: true`、
            # 来源显示"你在面板里设的"，而之后每一次模型调用都 401——坏在"看起来
            # 刚配好"上。静默当成"没改"也不行：用户以为保存成功，实际什么都没发生。
            # 宁可 400 说清楚，与 `clear_api_key` 撞车时同一条规矩。
            #
            # 两个比法都要：`current.api_key` 是库里那一行，密钥只来自环境变量时它
            # 是空的，而界面上那段打码值正是从环境变量那把算出来的——只比库会在
            # "密钥来自环境变量"这一路上漏掉，而这一路最疼：打码值一旦落库，就
            # 顶掉了环境变量里那把真密钥，且 `api_key_source` 会说"你在面板里设的"。
            raise SettingsError(
                "settings_api_key_masked",
                "提交的是打码后的密钥（形如 …abcd），不是密钥本身；"
                "要改密钥请重新粘贴完整的值，不改就别动这一栏。",
                status=400)
    next_model = (current.model if model is None
                  else _check_text(model, label="模型名", limit=MAX_MODEL_CHARS))
    next_deep = (current.deep_model if deep_model is None
                 else _check_text(deep_model, label="深度思考模型名", limit=MAX_MODEL_CHARS))

    # 密钥的锚点。三种情形，对应三件不同的事，与上面 `next_key` 那三段一一对应：
    #
    #   - 提交了新密钥 → 锚点就是**这次一起生效的那个地址**。用户粘贴密钥的那一刻
    #     已经认可了"这把钥匙给这台机器"。
    #   - 没提交新密钥 → 锚点**原样保留**。老行没有锚点时用旧地址算出来再写下来
    #     （`_binding_of` 的兜底），刻意落库而不是每次读时算：地址一变，落库的旧锚点
    #     就与新地址对不上，密钥自动不再交出。若把兜底留在读侧算，它用的会是
    #     **已经改过的新地址**，锚点跟着地址一起漂，绑定等于不存在。
    #   - 清空密钥 → 锚点一起清掉。留着它会让将来新填的密钥"继承"一个它没被验证过
    #     的地址。
    #
    # 判据是"**这次请求提交了密钥**"，不是"提交的密钥与库里那把不同"：`unbound` 的
    # 恢复动作就是"重新粘贴一次"，而用户粘回来的极可能就是同一把（他没换过供应商，
    # 只是换了地址）。拿 `next_key != current.api_key` 当判据时，这种最正常的恢复
    # 姿势会静默失败——界面提示"重新粘贴一次即可"，粘完仍是 `unbound`，而那时
    # 用户唯一能想到的解释是"这个功能坏了"。提交了密钥就是一次明确的"我认可这个
    # 地址"，与它是否等于旧值无关。攻击者这边不受影响：他改地址只能不带密钥，
    # 带密钥得先有密钥，而打码值那一道已经在上面挡掉了。
    old_host = _norm_text(current.api_key_host).lower() or _host_of(current.base_url)
    if clear_api_key:
        next_host = ""
    elif (api_key or "").strip():
        next_host = _host_of(next_base) or _host_of(os.getenv(BASE_URL_ENV, ""))
    else:
        next_host = old_host

    changed = (next_base, next_key, next_model, next_deep, next_host) != (
        current.base_url, current.api_key, current.model, current.deep_model,
        _norm_text(current.api_key_host).lower())
    if changed:
        try:
            with closing(_connect(timeout=SETTINGS_LOCK_TIMEOUT)) as conn, conn:
                conn.execute(
                    f"""INSERT INTO {TABLE}
                            (id, base_url, api_key, model, deep_model, updated_at, api_key_host)
                        VALUES (?, ?, ?, ?, ?, {_NOW}, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            base_url     = excluded.base_url,
                            api_key      = excluded.api_key,
                            model        = excluded.model,
                            deep_model   = excluded.deep_model,
                            updated_at   = excluded.updated_at,
                            api_key_host = excluded.api_key_host""",
                    (ROW_ID, next_base, next_key, next_model, next_deep, next_host),
                )
        except sqlite3.Error as exc:
            # 与 `_read_strict_for_save` 同一条规矩：写不进去也要给出能读的 code。
            # 裸 `OperationalError` 冒到 FastAPI 会变 500 + 一段 traceback，面板
            # 只能显示一句通用错误——而这条路上最可能发生的事恰恰是"过几秒就好了"。
            raise SettingsError(
                "settings_db_unavailable",
                "配置表正被占用，这次没有保存。请过几秒重试。",
                status=503) from exc
        # 写完立刻作废：这个功能的整个卖点是"保存即生效"，TTL 只能兜跨进程，
        # 不能给同进程的正常路径加延迟。
        _invalidate()
    return resolve(), changed


def clear() -> int:
    """清空配置行（只在用例与本地调试用）。返回删掉的行数。"""
    global _DEGRADED_WARNED
    _DEGRADED_WARNED = False
    with closing(_connect()) as conn, conn:
        rowcount = conn.execute(f"DELETE FROM {TABLE}").rowcount
    _invalidate()
    return rowcount


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
    "SETTINGS_LOCK_TIMEOUT",
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
