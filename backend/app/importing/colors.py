"""外部格式的视觉字段 → 我们的 `color` / `icon`。

**这个文件的核心原则是"不猜"**：宁可不映射，也不要把 `#7f7f7f` 猜成 `gray`、
把 XMind 的 `smiley-laugh` 猜成某个笑脸——一张 1000 节点的图上有 40 个点被我们
自作主张染色，用户没法一个个改回来，而"颜色不对"这种错他一眼看得见却说不清
出处。

所以三张表都是**精确匹配**：

    `_HEX_TO_COLOR`      Office / XMind / FreeMind 常见的那十几个色值。
                         3 位缩写先展开成 6 位，大小写折平，其余一律 None。
    `_FREEMIND_ICONS`    FreeMind 的 `BUILTIN` 名（它是一份封闭的枚举，
                         所以能精确映射）。
    `_XMIND_MARKERS`     XMind 的 `markerId`，同样封闭；只收语义明确的
                         （星标 / 旗标 / 完成 / 进行 / 优先级 / 心）。

映射不到的值**丢掉而不是落到某个默认值**：`None` 在契约里就是"没有颜色/图标"，
它在界面上表现为跟随上级或不着色——那是诚实的。落到 `gray` 会让所有没认出来的
颜色长得一样，反而看不出"这里丢过东西"。
"""
from __future__ import annotations

# 精确色表。键是归一化之后的 `#rrggbb`，值是契约 `NODE_COLORS` 里的键。
#
# 取色依据：Office 标准色（XMind / FreeMind 用户最常选的那一批）+ Material
# Design 的 500/700 档（第三方工具生成的默认配色）。**同一个键下不追求"色系
# 相邻"**，只收通用调色板里能一眼叫出名字的那几个色。
_HEX_TO_COLOR: dict[str, str] = {
    # blue：正蓝
    "#0000ff": "blue",
    "#0070c0": "blue",
    "#1e88e5": "blue",
    "#2196f3": "blue",
    "#3b82f6": "blue",
    "#1565c0": "blue",
    # green：正绿 / 草绿
    "#00b050": "green",
    "#00a651": "green",
    "#43a047": "green",
    "#4caf50": "green",
    "#2e7d32": "green",
    # amber：橙黄。"amber" 在契约里是"注意/待定"，橙与黄都属于这一档
    "#ffc000": "amber",
    "#ffb300": "amber",
    "#fdd835": "amber",
    "#ffa000": "amber",
    "#ff9800": "amber",
    # red：正红 / 深红
    "#ff0000": "red",
    "#c00000": "red",
    "#e53935": "red",
    "#f44336": "red",
    "#d32f2f": "red",
    # purple：紫
    "#7030a0": "purple",
    "#8e24aa": "purple",
    "#9c27b0": "purple",
    "#673ab7": "purple",
    # teal：青
    "#00b0f0": "teal",
    "#00acc1": "teal",
    "#0097a7": "teal",
    "#26a69a": "teal",
    # pink：粉
    "#ff66cc": "pink",
    "#ec407a": "pink",
    "#e91e63": "pink",
    "#f06292": "pink",
    # gray：中性
    "#808080": "gray",
    "#9e9e9e": "gray",
    "#757575": "gray",
    "#bdbdbd": "gray",
    "#a6a6a6": "gray",
}

# FreeMind 的 `BUILTIN` 图标名 → 一个 emoji。只收表里的：这份枚举本身是封闭的，
# 认不出的名字（插件自定义图标）会被丢掉。
_FREEMIND_ICONS: dict[str, str] = {
    "idea": "💡",
    "help": "❓",
    "question": "❓",
    "button_ok": "✅",
    "button_cancel": "❌",
    "yes": "✔️",
    "no": "✖️",
    "messagebox_warning": "⚠️",
    "stop-sign": "🛑",
    "important": "❗",
    "bookmark": "🔖",
    "attach": "📎",
    "desktop_new": "🆕",
    "flag": "🚩",
    "star": "⭐",
    "go": "➡️",
    "back": "⬅️",
    "up": "⬆️",
    "down": "⬇️",
    "clock": "⏰",
    "full-0": "⬜",
}

# XMind 的 `markerId` → emoji。前缀型（`star-red` / `flag-green`）按前缀收，
# 因为 XMind 对同一族的 marker 只换颜色，语义完全一样。
#
# **`priority-1..9` 故意不在表里**，虽然它语义明确：那几个 emoji 是「数字 +
# 变体选择符 + 组合键」（`1️⃣`），而契约的 `normalize_icon` 会把"含数字的值"
# 判成"写成文字的图标"并丢掉——那条规则是为"模型写 `icon: '放大镜'`"设的，
# 但它对数字 emoji 同样生效。为了一个装饰字段在导入层开一条特例，等于让"图标"
# 出现两套校验口径；不如统一按"映射不了就丢"处理。
_XMIND_MARKERS: dict[str, str] = {
    "heart": "❤️",
    "task-done": "✅",
    "task-start": "🔄",
}

# 前缀族：`star-*` → 星、`flag-*` → 旗。其余前缀（smiley / people / arrow /
# symbol / month / week / half / quarter）**故意不收**：它们在导图上要么读不出
# 语义，要么一旦映射就会让每个节点都挂一个看不懂的符号。
_XMIND_MARKER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("star-", "⭐"),
    ("flag-", "🚩"),
)


def normalize_hex(value: object) -> str | None:
    """`#f00` / `#FF0000` / `ff0000` → `#ff0000`；认不出返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if not text.startswith("#"):
        text = "#" + text
    body = text[1:]
    if len(body) == 3 and all(ch in "0123456789abcdef" for ch in body):
        return "#" + "".join(ch * 2 for ch in body)
    if len(body) == 6 and all(ch in "0123456789abcdef" for ch in body):
        return text
    return None


def palette_for_hex(value: object) -> str | None:
    """任意色值 → 调色板键；表里没有就是 `None`（**不猜**）。"""
    normalized = normalize_hex(value)
    if normalized is None:
        return None
    return _HEX_TO_COLOR.get(normalized)


def freemind_icon(builtin: object) -> str | None:
    """FreeMind 的 `BUILTIN` → emoji；不认识返回 None。"""
    if not isinstance(builtin, str):
        return None
    return _FREEMIND_ICONS.get(builtin.strip().lower())


def xmind_icon(marker_id: object) -> str | None:
    """XMind 的 `markerId` → emoji；不认识返回 None。"""
    if not isinstance(marker_id, str):
        return None
    key = marker_id.strip().lower()
    exact = _XMIND_MARKERS.get(key)
    if exact is not None:
        return exact
    for prefix, emoji in _XMIND_MARKER_PREFIXES:
        if key.startswith(prefix):
            return emoji
    return None
