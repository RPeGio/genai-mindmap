"""PDF 云端解析：阿里云文档智能（DocMind）。

为什么留这条路：Docling 的版面模型常驻内存 1.2GB，2C2G 的部署机跑不动；
pypdf 虽然只要几十 MB，但双栏会串行、表格会散。DocMind 把版面理解放到
云端，本地只剩一次 HTTP 上传和下载，内存几乎不涨，输出还是 Markdown。

调用形态是异步任务，要三步走：

    SubmitConvertPdfToMarkdownJob  ->  拿到任务 Id
    GetDocumentConvertResult       ->  轮询到 Success，拿到结果文件 URL
    GET 那个 URL                   ->  得到 Markdown

文件直接用请求体上传（SDK 的 *Advance* 接口支持传二进制流），
不需要先开通 OSS，也不需要文件有公网地址。

**默认不走这条链路**：它会按页计费，自动选中等于替用户花钱。
只有显式设置 MINDMAP_PDF_PARSER=docmind 才启用，见 ingestion/service.py。
"""
from __future__ import annotations

import io
import time

DOCMIND_ENDPOINT = "docmind-api.cn-hangzhou.aliyuncs.com"

# 轮询节奏。文档智能一般 3–15 秒出结果，2 秒一次既能早拿结果又不会把
# 接口打成刷子；总时长兜到 180 秒，够 50 页的文档。
_POLL_INTERVAL_SECONDS = 2.0
_TIMEOUT_SECONDS = 180.0

# 结果文件下载上限。Markdown 比 PDF 小得多，正常几十 KB；
# 设个上限纯粹是防止拿到一个意外的大响应把内存吃掉。
_MAX_RESULT_BYTES = 8 * 1024 * 1024


class DocMindUnavailable(Exception):
    """云端解析不可用：未配置凭据、服务未开通、或任务失败。

    单独定义而不是复用 NotSupportedSource：这一层不依赖 ingestion 的
    其余代码，service 那边负责翻译成对用户说的话。
    """


# 任务尚未完成时的 Code。实测「还在跑」的那一轮返回的是
#   {"Code": "DocProcessing", "Completed": false, "Data": [], "Status": "Fail"}
# 也就是说 Status 会是 "Fail" —— 只按 Status 判断会把正常处理中的任务误判为失败。
_PENDING_CODES = {"", "Success", "DocProcessing", "DocMindResultNotReady"}
_PENDING_STATUSES = {"processing", "running", "pending", "queued"}


def _sdk():
    """延迟导入 SDK。

    没装 SDK 的部署（比如只跑 DOCX 的开发机）不该在 import 阶段就崩，
    等真的要用云端解析时再报一句人话。
    """
    try:
        from alibabacloud_docmind_api20220711 import models as dm
        from alibabacloud_docmind_api20220711.client import Client
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_tea_util import models as util_models
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的部署上触发
        raise DocMindUnavailable(
            "未安装阿里云文档智能 SDK，请 pip install "
            "alibabacloud_docmind_api20220711") from exc
    return dm, Client, open_api_models, util_models


def _friendly_error(code: str, message: str) -> str:
    """把接口错误码翻成一句用户能照着做的话。"""
    if code == "DocMindServiceNotOpen":
        return "阿里云文档智能服务尚未开通，请先在控制台开通该服务再重试"
    if code in ("InvalidAccessKeyId.NotFound", "InvalidAccessKeyId",
                "SignatureDoesNotMatch"):
        return "文档智能的 AccessKey 无效或权限不足，请检查 MINDMAP_DOCMIND_AK / _SK"
    if code == "Forbidden.AccessDenied":
        return "当前 AccessKey 没有调用文档智能的权限，请为该 RAM 用户授权"
    return f"文档智能调用失败（{code}）：{message or '未返回详细信息'}"


def _download(url: str) -> str:
    import httpx

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        if len(resp.content) > _MAX_RESULT_BYTES:
            raise DocMindUnavailable("云端返回的解析结果异常大，已放弃读取")
        return resp.text


def parse_pdf_via_docmind(filename: str | None, raw: bytes,
                          ak: str, sk: str) -> str:
    """把 PDF 交给文档智能转成 Markdown，返回正文。

    这是一次完整的「提交 + 轮询 + 下载」，同步阻塞，调用方必须放到线程里跑
    （router 那边已经用 run_in_threadpool 包住了）。
    """
    dm, Client, open_api_models, util_models = _sdk()

    config = open_api_models.Config(access_key_id=ak, access_key_secret=sk)
    config.endpoint = DOCMIND_ENDPOINT
    client = Client(config)

    request = dm.SubmitConvertPdfToMarkdownJobAdvanceRequest(
        file_name=filename or "upload.pdf",
        file_url_object=io.BytesIO(raw),
    )

    try:
        submitted = client.submit_convert_pdf_to_markdown_job_advance(
            request, util_models.RuntimeOptions())
    except Exception as exc:  # noqa: BLE001 - SDK 异常层次不稳定，统一转译
        raise DocMindUnavailable(f"提交解析任务失败：{exc}") from exc

    body = submitted.body
    code = getattr(body, "code", "") or ""
    if code and code != "Success":
        raise DocMindUnavailable(_friendly_error(code, getattr(body, "message", "")))

    task_id = getattr(getattr(body, "data", None), "id", None)
    if not task_id:
        raise DocMindUnavailable("提交解析任务失败：接口没有返回任务 Id")

    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            queried = client.get_document_convert_result(
                dm.GetDocumentConvertResultRequest(id=task_id))
        except Exception as exc:  # noqa: BLE001
            raise DocMindUnavailable(f"查询解析进度失败：{exc}") from exc

        result = queried.body
        code = getattr(result, "code", "") or ""
        status = (getattr(result, "status", "") or "").lower()
        message = getattr(result, "message", "") or ""

        # 有下载地址就是完成，别的字段一概不看。
        url = _result_url(getattr(result, "data", None))
        if url:
            return _download(url)

        # 任务还在跑时，接口会把 Status 报成 "Fail"、Code 报成 "DocProcessing"，
        # Data 还是空数组。按 Status 判断会把它误杀，所以先按 Code 放行。
        if code in _PENDING_CODES or status in _PENDING_STATUSES:
            continue

        raise DocMindUnavailable(_friendly_error(code, message))

    raise DocMindUnavailable(
        f"文档智能在 {int(_TIMEOUT_SECONDS)} 秒内没有返回结果，请稍后重试或改用本地解析")


def _result_url(data) -> str | None:
    """从结果里挑出 Markdown 文件的下载地址。

    Data 可能是单个对象也可能是数组（一份 PDF 可能产出多个文件），
    优先取 Type 为 md 的那个；没有 Type 信息就退而取第一个带 Url 的。
    """
    if data is None:
        return None
    items = data if isinstance(data, list) else [data]

    fallback = None
    for item in items:
        url = getattr(item, "url", None)
        if not url:
            continue
        kind = (getattr(item, "type", "") or "").lower()
        if kind in ("md", "markdown"):
            return url
        if fallback is None:
            fallback = url
    return fallback


def page_count_of_pdf(raw: bytes) -> int | None:
    """只读页面树拿页数，不抽文字，开销可以忽略。

    云端结果里没有页数字段，而 SourceSpan.page 需要它；与其为这一件事
    再发一次请求，不如本地数一下。
    """
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:  # noqa: BLE001 - 页数只是附带信息，拿不到就算了
        return None
