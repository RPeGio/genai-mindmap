"""导入过程中的两类错误。

**为什么与 `app.core.errors.AppError` 分开**：`AppError` 是"带 HTTP 状态和业务
code 的出口"，而解析器只该回答"这份文件怎么了"。`unsupported_format` 与
`parse_failed` 这两个码的归属在 router 层决定（见 `service.parse_import` 的
docstring），解析器里出现 `status=400` 只会让"某个解析器想报 500"这种事没有
表达方式。

`UnsupportedFormat` 与 `ParseFailure` 的界线是本模块唯一需要记的规则：

    - **UnsupportedFormat**：字节没错，但它不是我们要的格式。典型是 `.docx`
      ——它确实是 zip，可里面没有 XMind 的 `content.json`/`content.xml`；
      再比如一份 `.json` 里既没有 `root` 也没有节点。
    - **ParseFailure**：格式对了，文件坏了。zip 中央目录损坏、JSON 少一个引号、
      XML 标签不闭合、markdown 里一个列表项都没有。

这条界线决定用户拿到的是"暂不支持这种文件"还是"文件第 12 行解析失败"——后者
可操作，前者不可。所以能定位到行的时候一定写进行文里。
"""
from __future__ import annotations


class ImportFailure(Exception):
    """两类失败的公共基类，只为了让 router 一次接住。"""


class UnsupportedFormat(ImportFailure):
    """认得出字节，但它不是我们能导入的格式。"""

    code = "unsupported_format"


class ParseFailure(ImportFailure):
    """格式对，内容坏了。message 里尽量带行号或节点名。"""

    code = "parse_failed"
