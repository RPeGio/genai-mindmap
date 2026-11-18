import type { Source, SourceKind } from '../api/types';

// 与 ingestion/router.py 以及 agent 侧的来源上限保持一致。
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
export const MAX_SOURCES = 50;
/**
 * 合并后的字数上限，与后端 `CONTEXT_TOKEN_LIMIT`（200000 token）对齐。
 *
 * 后端按「汉字 1.5 字/token、ASCII 4 字符/token」估算，200000 token 对应纯中文
 * 约 30 万字、纯英文约 80 万字符。这里取纯中文那个最费 token 的数字：中英混排
 * 时前端会比后端更早拦一点，方向是保守的，不会出现"前端放行、后端 422"。
 *
 * 原先这里卡的是 20000 字。那个数是照一句话主题拍的，没有实测依据——一篇十五页
 * 的论文（Attention Is All You Need 转成正文约 38000 字符）连一半都放不进去，
 * 而它换算下来才九千多 token，离上限差二十倍。后端的字数闸早就删了，前端这道
 * 一直留着，于是一篇正常论文在界面上就生成不了。
 */
export const MAX_INPUT_CHARS = 300000;
/**
 * 材料总字数低于这个数时，后端**不把它当语料**：改为"主题提示 + 联网检索（或纯
 * 模型知识）"。理由在后端 `tools.SHORT_MATERIAL_CHARS` 那段（300 = 一条引用的
 * 字段上限：整份材料塞得进一条引用时，引用不再"指向某处"）。
 *
 * 前端镜像这个数的唯一用途是**判断那个"联网检索"开关还灵不灵**：材料够长时这次
 * 一定不走外部检索，勾了也没用，界面得如实置灰而不是让用户以为它坏了。它只影响
 * 一句提示，判定权威始终在后端——两边分叉时表现为"提示不准"，不会表现为"生成错了"。
 */
export const SHORT_MATERIAL_CHARS = 300;
export const FILE_ACCEPT = '.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.md,.asciidoc,.adoc,.wav,.png,.jpg,.jpeg,.webp,.gif,.bmp';

export const SOURCE_LABELS: Record<SourceKind, string> = {
  text: '文本', markdown: 'Markdown', document: '文档', image: '图片', audio: '语音',
  // 外部检索抓回来的两类。文案要明确说"外"，否则用户会以为这是自己上传的材料
  // ——两者的可信度与责任归属都不一样。
  web: '网页', paper: '论文',
};

let sourceSequence = 0;

/** HTTP 局域网访问时 randomUUID 可能不可用，文本和文件输入仍应能使用。 */
export function newSourceId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `source_${Date.now().toString(36)}_${++sourceSequence}_${Math.random().toString(36).slice(2)}`;
}

export function fileKind(file: File): SourceKind {
  const extension = file.name.split('.').pop()?.toLowerCase();
  if (extension === 'wav') return 'audio';
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'].includes(extension ?? '')) return 'image';
  return 'document';
}

/** Python len 按 Unicode 码点计数；包含来源之间的两个换行，不能只累加正文。 */
export function mergedCharCount(sources: Source[]): number {
  return Array.from(sources.map(source => source.text?.trim() ?? '').join('\n\n')).length;
}

export function sourceInputError(sources: Source[]): string {
  if (!sources.length) return '请先添加至少一个来源。';
  if (sources.length > MAX_SOURCES) return `最多添加 ${MAX_SOURCES} 个来源，请移除部分来源。`;
  if (new Set(sources.map(source => source.id)).size !== sources.length) return '来源 ID 重复，请移除重复来源后重试。';
  if (sources.some(source => source.status !== 'parsed' || !source.text?.trim())) {
    return '有来源尚未解析成功或内容为空，请重试或移除后生成。';
  }
  const count = mergedCharCount(sources);
  return count > MAX_INPUT_CHARS
    ? `合并后内容为 ${count} 字，超过当前 ${MAX_INPUT_CHARS} 字限制；请缩短或分批处理。`
    : '';
}

export function textSource(text: string): Source {
  return {
    id: newSourceId(), kind: 'text', filename: null, text: text.trim(),
    span: null, status: 'parsed', error: null, parsed_pages: null,
    truncated: false, failed_pages: [],
  };
}

// ---- 材料标题能不能当主题 ---------------------------------------------------
//
// 「只上传材料、不写主题」是很自然的用法：用户上传一份 PDF，期待的就是"把它整理成
// 导图"。后端的 `mindmap_generate_deep` 只需要一个 `topic` 字符串，所以标题可以由
// 前端补上——但**只能是材料标题里真的带信息的那种**。
//
// `未命名.txt` / `扫描件.pdf` / `IMG_001.png` 的长度都过得了"非空"这道关，可它们
// 描述的是**文件**而不是**内容**。拿它当中心主题，模型只能围绕"未命名"猜，产出一张
// 以垃圾词为根的图——这比直接要求用户补一句主题更糟：他想不通自己错在哪。
// 所以判据是"这个标题有没有携带信息"，不满足就退回「请写一个主题」。

/** 与 `FILE_ACCEPT` 同一份扩展名。只认这一份白名单，不按"最后一个点"切。 */
const TITLE_EXTENSIONS = [
  '.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
  '.html', '.htm', '.md', '.asciidoc', '.adoc', '.txt',
  '.wav', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp',
];

/**
 * 通用词：描述的是文件本身，不是内容。归一化（去空白、小写）之后**全等**才算命中。
 *
 * 不做子串判断：`机器学习文档` 命中 `文档` 就把一个真标题判死，而它明明能用。
 */
const GENERIC_TITLE_WORDS = new Set([
  '未命名', '无标题', '新建', '新建文档', '新建文本文档', '文档', '文件', '附件',
  '扫描件', '扫描', '图片', '照片', '截图', '屏幕截图', '截屏', '录屏', '录音', '音频', '语音',
  '草稿', '副本', '测试', '临时', '主题', '材料', '资料', '内容', '示例',
  'untitled', 'unnamed', 'document', 'doc', 'file', 'attachment', 'scan', 'image', 'img',
  'photo', 'screenshot', 'screen shot', 'audio', 'voice', 'draft', 'copy', 'test', 'tmp',
  'temp', 'new', 'sample', 'demo',
]);

/** 设备/文件编号式名字：`IMG_001`、`DSC1234`、`微信截图_20241117`。 */
const DEVICE_NAME = /^(img|dsc|dscn|dscf|pxl|mvimg|screenshot|scan|wx|mmexport|screenshot)[-_ ]?\d+$/;

/** 主题至少两个字、至多三十个字：一个字的标题当不了中心主题，超长的多半是文件名。 */
export const TOPIC_MIN_CHARS = 2;
export const TOPIC_MAX_CHARS = 30;

export interface MaterialTitle {
  /** 能当主题的标题（已去扩展名）；空串 = 这批材料里没有能用的。 */
  topic: string;
  /** 标题取自哪份材料；`topic` 非空时才有。 */
  from: string;
  /** 没有可用标题时的一句话原因，`topic` 非空时是空串。 */
  reason: string;
}

/** 文件名去掉扩展名。`方案 v1.2` 这种名字不能被切成 `方案 v1`。 */
export function fileTitle(filename: string): string {
  const name = filename.trim();
  const lower = name.toLowerCase();
  const hit = TITLE_EXTENSIONS.find(extension => lower.endsWith(extension));
  return (hit ? name.slice(0, name.length - hit.length) : name).trim();
}

/** 剪掉「(2)」「- 副本」这类后缀，再压掉多余空白。 */
function normalizeTitle(stem: string): string {
  return stem
    .replace(/\s*[（(]\d+[)）]\s*$/, '')
    .replace(/[-_ ]*(副本|拷贝|copy)\s*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** 这个标题为什么当不了主题；空串 = 能用。 */
function titleProblem(title: string): string {
  const chars = Array.from(title).length;
  if (!chars) return '材料标题是空的';
  if (chars < TOPIC_MIN_CHARS) return `材料标题「${title}」太短`;
  if (chars > TOPIC_MAX_CHARS) return '材料标题太长，不像一个主题';
  const key = title.toLowerCase();
  if (GENERIC_TITLE_WORDS.has(key)) return `材料标题「${title}」是通用词`;
  if (DEVICE_NAME.test(key.replace(/\s+/g, ''))) return `材料标题「${title}」像个文件编号`;
  // 去掉数字/空白/标点之后还剩什么：`20241117`、`第 1 章` 剩下的都不够两个字。
  const letters = Array.from(title.replace(/[\d\s\p{P}\p{S}]/gu, ''));
  if (letters.length < TOPIC_MIN_CHARS) return `材料标题「${title}」里只有数字或符号`;
  return '';
}

/**
 * 从材料里挑一个能当主题的标题。
 *
 * 按顺序取**第一份可用**的：文件名才是标题的载体（粘贴的文本来源没有 `filename`，
 * 它天然给不出主题）。第一份不可用时会继续往后看——用户传了 `未命名.txt` 与
 * `机器学习导论.pdf`，用后者当主题显然比逼他再写一句主题更省事。
 */
export function materialTitle(sources: Source[]): MaterialTitle {
  const titled = sources.filter(source => (source.filename ?? '').trim());
  if (!titled.length) {
    return {
      topic: '',
      from: '',
      reason: sources.length ? '粘贴的材料没有标题' : '',
    };
  }
  let firstProblem = '';
  for (const source of titled) {
    const title = normalizeTitle(fileTitle(source.filename ?? ''));
    const problem = titleProblem(title);
    if (!problem) return { topic: title, from: source.filename ?? '', reason: '' };
    // 只留第一份的原因：那是用户最先看到、也最容易认出来的那个名字。
    if (!firstProblem) firstProblem = problem;
  }
  return { topic: '', from: '', reason: firstProblem };
}

/** 每份来源单独提示缺页与截断，不能被随后加入的文件覆盖。 */
export function sourceWarning(source: Source): string {
  const parts: string[] = [];
  const failed = source.failed_pages ?? [];
  if (failed.length) {
    const shown = failed.slice(0, 5).map(page => `第 ${page} 页`).join('、');
    parts.push(`${shown}${failed.length > 5 ? ` 等 ${failed.length} 页` : ''} 识别失败，这些页未包含在来源正文中`);
  }
  if (source.truncated) {
    const total = source.span?.page;
    const parsed = source.parsed_pages;
    if (typeof total === 'number' && typeof parsed === 'number') {
      parts.push(failed.length
        ? `本文档共 ${total} 页，成功解析 ${parsed} 页，超出页数上限的尾部未处理`
        : `本文档共 ${total} 页，仅处理前 ${parsed} 页`);
    } else {
      parts.push('文档较长，本次只处理了一部分内容');
    }
  }
  return parts.join('；');
}
