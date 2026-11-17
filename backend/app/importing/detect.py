"""格式判定：扩展名 + 内容嗅探，内容赢。

为什么两套判据都要，而不是只看扩展名或只看内容：

  - **只看扩展名**会漏掉真实场景里最常见的一类文件：浏览器下载时被改名、
    用户手动改成 `.txt` 再上传、或者导出工具根本不写扩展名。内容对，就该读得
    进来。
  - **只看内容**会把 `.docx`（也是 zip）、`.html`、`.csv` 当成思维导图去解析，
    报出来的错误是"第 1 行解析失败"——而用户根本没做错什么。

所以判定顺序是：

  1. **魔数优先**。`PK\\x03\\x04` 开头一律当 XMind 候选（题目里那句"`.json`
     但有 zip 魔数 → 其实是 xmind"就是这条）。zip 里到底有没有 XMind 的成员，
     只有打开才知道，所以这里只是**候选**，最终确认在 `xmind.parse`：它打不开
     或成员不对时，`parse_import` 会把 `UnsupportedFormat` 原样报成
     `unsupported_format`。把"确认"留在能打开文件的那一层，是为了不把
     `detect_format(filename, head_bytes)` 变成"读完整个文件"。
  2. **文本特征**。按"特征强度"排：`<opml` 只可能出现在 OPML；`<map` + `<node`
     是 FreeMind；`<xmap-content` 是 XMind 8 的 `content.xml`（允许用户把单个
     xml 直接拖进来）；`@startmindmap` 是 PUML；`{` / `[` 是 JSON。这五个是
     **强特征**——它们只可能来自那一种格式，所以能推翻扩展名。
  3. **扩展名兜底**。内容什么都认不出来时，扩展名告诉我们"用户打算给什么"，
     于是错误能说"你的 `.mm` 里没有 `<map>`"，而不是"未知格式"。
  4. **markdown 的嗅探是最弱的一档**，排在扩展名之后（理由见 `detect_format`）。
  5. 都不成立 → `None`（router 报 `unsupported_format`）。

`.txt` 是唯一一个**只在内容也像大纲时**才算数的扩展名：纯文本是所有人的默认
格式，放行它等于把所有误传的文件都送进 markdown 解析器。
"""
from __future__ import annotations

# zip 的四种签名：普通成员开始、空归档结束、跨越分卷、数据描述符。
# 只看第一种会漏掉"用户上传了一个空 xmind"——那种文件是 22 字节的
# `PK\x05\x06` 结尾记录，按未知格式报出来比按损坏报出来更难解释。
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"PK\x06\x06")

_SUFFIX_FORMATS = {
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdown": "markdown",
    ".mkd": "markdown",
    # `.txt` **不在这张表里**：纯文本是所有人的默认格式，放行它等于把所有误传的
    # 文件都送进 markdown 解析器。它只在内容也像大纲时才算数（走嗅探那一档）。
    ".opml": "opml",
    ".xmind": "xmind",
    ".mm": "freemind",
    ".puml": "puml",
    ".plantuml": "puml",
    ".pu": "puml",
    ".iuml": "puml",
}

# 嗅探只看头部。8KB 足够覆盖"标题 + 前几十行"，也不会让一次上传读两遍。
SNIFF_BYTES = 8192


def suffix_of(filename: str) -> str:
    """小写扩展名（带点）。没有扩展名时返回空串。"""
    name = (filename or "").strip()
    # 只看最后一段：`C:\a.b\c` 这种路径里含有"点"的目录名不该被当成扩展名。
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return ("." + base.rsplit(".", 1)[-1]).lower()


def _sniff_text(head: bytes) -> str | None:
    """从头部字节里认格式。认不出来返回 None。

    不做完整编码检测：这里只需要"文本像什么"，而三种候选编码（UTF-8 / UTF-16 /
    GB18030）的 ASCII 区段在头部是兼容的，标记与标签名都读得出来。真正的解码
    在 `text.decode_text` 里做，那里才需要严格。
    """
    text = head
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if text.startswith(bom):
            text = text[len(bom):]
            break
    try:
        # utf-16 的头部在按 utf-8 解时会得到一堆 NUL，`lstrip()` 之后仍然能看出
        # 有没有标记；用 errors="ignore" 让任何编码都不至于把嗅探搞崩。
        probe = text.decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, ValueError):  # pragma: no cover - ignore 不会抛
        return None
    stripped = probe.lstrip()
    if not stripped:
        return None

    lowered = stripped[:SNIFF_BYTES].lower()
    # XML 家族先判具体根元素，再判"这是 XML"——顺序反了会把 opml 判成通用 xml。
    if "<opml" in lowered:
        return "opml"
    if "<xmap-content" in lowered:
        # XMind 8 的 content.xml。允许单文件直接导入：用户从 .xmind 里解出来的
        # 只有这一个文件，而它自证是 XMind 的内容表，没必要逼他重新打包。
        return "xmind"
    if "<map" in lowered and ("<node" in lowered or "text=" in lowered):
        return "freemind"
    if "@startmindmap" in lowered:
        return "puml"
    if stripped[0] in "{[":
        return "json"
    if _looks_like_outline(stripped):
        return "markdown"
    return None


def _looks_like_outline(text: str) -> bool:
    """`# 标题` 或 `- 项目` 开头的多行文本。

    要求至少一行标题/列表项**行首**成立，而不是"任意位置包含 `- `"：后者会把
    一封普通的邮件正文判成大纲，用户拿到的错误就变成"没找到任何列表项"。
    """
    checked = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        checked += 1
        head = line.lstrip()
        if head.startswith("#") or head.startswith(("- ", "* ", "+ ")):
            return True
        # `1. 标题` / `2）标题`。只认一位到两位数字，避免把年份、价格当序号。
        cut = 0
        while cut < len(head) and cut < 2 and head[cut].isdigit():
            cut += 1
        if cut and cut < len(head) and head[cut] in ".、)）" and \
                cut + 1 < len(head) and head[cut + 1] == " ":
            return True
        if checked >= 40:
            break
    return False


def is_zip(head: bytes) -> bool:
    return any(head.startswith(magic) for magic in _ZIP_MAGICS)


def detect_format(filename: str, head_bytes: bytes) -> str | None:
    """`filename` + 文件头 → 格式名；认不出来返回 `None`。

    判定顺序（`head` 是候选，最终确认在 `xmind.parse`）：

      1. zip 魔数 → `xmind`。
      2. **强内容特征**（`<opml` / `<map>` / `<xmap-content` / `@startmindmap` /
         `{`）→ 内容赢。这类特征只可能来自那一种格式，扩展名写错也该按内容读。
      3. 扩展名在表里 → 用它。
      4. **markdown 的嗅探是兜底，不是强证据**。`* 根` / `- 项` / `# 标题` 在
         PlantUML、普通笔记、聊天记录里都常见，让它推翻扩展名会带来一个很糟的
         结果：`.puml` 文件缺 `@startmindmap` 时被当成 markdown 读成一份**层级
         丢失**的大纲（`** 子` 在 markdown 里是根的同级项），用户拿到一张结构
         错乱但"导入成功"的图。
      5. `.txt` / 无扩展名 + 像大纲 → markdown。
      6. 都不成立 → `None`。
    """
    head = head_bytes or b""
    if is_zip(head):
        return "xmind"

    sniffed = _sniff_text(head[:SNIFF_BYTES])
    if sniffed is not None and sniffed != "markdown":
        return sniffed

    suffix = suffix_of(filename)
    if suffix in _SUFFIX_FORMATS:
        return _SUFFIX_FORMATS[suffix]
    if sniffed is not None:
        return sniffed
    return None


def zip_member(names: list[str], wanted: str) -> str | None:
    """在 zip 成员表里找 `wanted`：先精确匹配根目录，再匹配任意层级。

    为什么允许任意层级：XMind 的某些版本把内容放在 `xmind/content.json` 之类
    的子目录里（以及第三方工具重新打包时）。精确匹配优先，是为了"同时存在两个
    content.json"时取根目录那个——那是 XMind 自己的写法。
    """
    if wanted in names:
        return wanted
    for name in names:
        if name.rsplit("/", 1)[-1] == wanted:
            return name
    return None
