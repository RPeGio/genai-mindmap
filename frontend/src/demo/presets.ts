/**
 * 演示用的预置导图。
 *
 * 为什么要有：演示现场的网络、模型额度、上游抖动都不受控，而"模型这一次没出图"
 * 会直接让演示断掉。这里放几张内容完整、可离线加载的图，界面上给一个按钮手动点。
 *
 * 三条约定：
 * 1. **不自动回退**。模型失败时仍然如实报错——把预置图偷偷顶上会让人分不清哪张
 *    是真的生成的，演示反而更难讲。加载入口只能是用户自己点。
 * 2. **始终打包进生产产物**（不进 `import.meta.env.DEV` 分支），但用动态 import
 *    单独成块：主包不因为演示数据变大，点按钮时才去取。
 * 3. 内容按"讲得清"写，不是按"塞得满"写：`detail` 是给画布悬停与右侧面板看的
 *    说明，`note` 是"我的备注"，两者都按契约 v2 的字段填，导出时正文会折进标题。
 */
import type { Direction } from '../canvas/layout/tidy';
import type { MindMapContent, MindNode } from '../api/types';

export interface DemoPreset {
  id: string;
  /** 按钮上的短名字 */
  label: string;
  /** 一句话说明这张图讲什么，给演示的人挑 */
  description: string;
  direction: Direction;
  content: MindMapContent;
}

interface NodeOptions {
  children?: MindNode[];
  detail?: string;
  note?: string;
}

function node(id: string, label: string, options: NodeOptions = {}): MindNode {
  const out: MindNode = { id, label, children: options.children ?? [] };
  if (options.detail) out.detail = options.detail;
  if (options.note) out.note = options.note;
  return out;
}

function map(title: string, root: MindNode): MindMapContent {
  return { title, schema_version: 2, root };
}

/** Rust 学习路线：五条主线，叶子带说明，适合讲"层级 + 正文"两层结构。 */
const RUST: MindMapContent = map('Rust 学习路线', node('rust', 'Rust 学习路线', {
  children: [
    node('rust-start', '先跑起来', {
      children: [
        node('rust-toolchain', '装工具链', {
          detail: '用 rustup 装，跟着版本走；rustc 是编译器，cargo 是包管理和构建入口。',
        }),
        node('rust-first', '第一段代码', {
          detail: 'cargo new 建项目、cargo run 跑起来，先别急着学所有权——先能改一行看一次结果。',
        }),
        node('rust-errors', '读编译器的话', {
          detail: 'Rust 的报错会给出建议改法，把它当成随身的老师，比查博客快。',
        }),
      ],
    }),
    node('rust-own', '所有权与借用', {
      children: [
        node('rust-own-rule', '所有权三条规则', {
          detail: '每个值只有一个所有者；离开作用域即释放；赋值和传参是移动而不是拷贝。',
        }),
        node('rust-borrow', '借用与生命周期', {
          detail: '同一时刻要么多个不可变借用、要么一个可变借用；生命周期标注是在告诉编译器借用能活多久。',
        }),
        node('rust-pitfall', '常见坑', {
          detail: '在持有可变借用时又去读同一个值；返回局部变量的引用；用 clone 掩盖设计问题。',
        }),
      ],
    }),
    node('rust-type', '类型系统与抽象', {
      children: [
        node('rust-struct', '结构体与枚举', {
          detail: '用枚举把"几种互斥情况"建成一个类型，match 时编译器会替你检查有没有漏。',
        }),
        node('rust-trait', 'trait 与泛型', {
          detail: 'trait 描述能力，泛型把它编译期展开；需要运行时多态时再用 dyn。',
        }),
        node('rust-result', '错误处理', {
          detail: 'Result 把失败当返回值，? 负责向上传递；panic 只留给"程序写错了"。',
        }),
      ],
    }),
    node('rust-conc', '并发与异步', {
      children: [
        node('rust-thread', '线程与 Send/Sync', {
          detail: 'Send 表示能跨线程移动，Sync 表示能被多个线程共享；编译器按这两个标记拒绝不安全的写法。',
        }),
        node('rust-async', 'async/await', {
          detail: 'async 只是把函数变成状态机，得有运行时（tokio 之类）来驱动它。',
        }),
        node('rust-share', '共享状态', {
          detail: 'Arc<Mutex<T>> 是最常见的组合：Arc 管所有权，Mutex 管互斥。',
        }),
      ],
    }),
    node('rust-eng', '工程实践', {
      children: [
        node('rust-workspace', '工作区与依赖', {
          detail: '多 crate 用 workspace 统一版本；依赖树用 cargo tree 看清楚谁带进来的。',
        }),
        node('rust-test', '测试与文档', {
          detail: '单元测试写在模块里，集成测试放 tests/；文档注释里的示例会被当作测试跑。',
        }),
        node('rust-lint', 'clippy / fmt / CI', {
          detail: '提交前跑 clippy 和 fmt，把风格争论交给工具，评审只看设计。',
        }),
      ],
    }),
    node('rust-project', '做一个小项目', {
      children: [
        node('rust-pick', '选型', {
          detail: '命令行工具或一个小的 HTTP 服务都行，关键是能端到端跑完一次。',
        }),
        node('rust-split', '拆模块与错误类型', {
          detail: '先按职责拆模块，再定义一个 crate 自己的错误枚举，别把 String 当错误到处传。',
        }),
        node('rust-ship', '发布', {
          detail: 'cross 编译多平台产物，README 写清怎么装怎么用。',
        }),
      ],
    }),
  ],
}));

/** RAG 方案选型：一条主线讲取舍，叶子给结论，适合回答"你们凭什么这么选"。 */
const RAG: MindMapContent = map('RAG 方案选型', node('rag', 'RAG 方案选型', {
  children: [
    node('rag-chunk', '切块策略', {
      note: '结论：递归切块起步，语义切块留到有评测集之后再说。',
      children: [
        node('rag-chunk-fixed', '固定长度', {
          detail: '最简单，但会把一句话劈开；作为基线可以用，别当最终方案。',
        }),
        node('rag-chunk-recursive', '递归切块', {
          detail: '按标题、空行、句号逐级退化，切点落在语义边界上，实测最稳。',
        }),
        node('rag-chunk-parent', '父块召回', {
          detail: '命中子块、喂父块，能补上下文；代价是逐字引用校验会退化成"落在父块里就算过"。',
        }),
      ],
    }),
    node('rag-retrieve', '检索', {
      children: [
        node('rag-bm25', '关键词', {
          detail: '专有名词、接口名、字段名这类精确词，关键词检索比向量稳。',
        }),
        node('rag-vector', '向量', {
          detail: '同义改写、口语化提问靠向量；缺点是会"看起来很像但答非所问"。',
        }),
        node('rag-hybrid', '混合 + RRF', {
          detail: '两路各取若干条，用倒数排名融合；比调单一阈值省事，坏天气下也更稳。',
        }),
      ],
    }),
    node('rag-rerank', '重排与过滤', {
      children: [
        node('rag-cross', '交叉编码器重排', {
          detail: '把 query 与候选一起看，精度明显好于向量相似度；代价是每条候选一次前向。',
        }),
        node('rag-threshold', '相关性阈值', {
          detail: '逐段过滤能提纯上下文，但会把"勉强相关"的段落也删掉，实测 token 降不下来太多。',
        }),
      ],
    }),
    node('rag-cite', '生成与引用', {
      note: '对外讲的重点：引用必须能逐字核对，模型说的不算。',
      children: [
        node('rag-verify', '逐字校验', {
          detail: '把 quote 拿回原文做子串匹配，对不上的引用一律剔除——引用准确率不能靠提示词保证。',
        }),
        node('rag-empty', '没有依据时怎么说', {
          detail: '允许按自己的知识补内容，但不许冒充材料；界面要明确标出"这段没有出处"。',
        }),
      ],
    }),
    node('rag-eval', '评测', {
      children: [
        node('rag-corpus', '语料与金标准', {
          detail: '固定一份材料 + 人工标好的要点清单，改一次参数就跑一遍。',
        }),
        node('rag-metric', '看哪几个数', {
          detail: '召回率（该找到的找到没）、引用准确率（写的出处在不在原文里）、以及端到端耗时。',
        }),
      ],
    }),
  ],
}));

/** 产品评审结论：五条并列分支，用来看双向布局。 */
const REVIEW: MindMapContent = map('一次产品评审的结论', node('review', '一次产品评审的结论', {
  children: [
    node('review-scope', '定位收敛', {
      detail: '这版只服务一类人：要快速把材料变成能讲的图，别顺手做成通用白板。',
    }),
    node('review-first-screen', '首屏体验', {
      detail: '打开就能输入，别让人先注册；生成过程要有进度，而不是干等一个转圈。',
    }),
    node('review-collab', '协作改造', {
      detail: '本轮只做"保存冲突能发现、能选"，不做多人实时同编。',
    }),
    node('review-data', '数据与埋点', {
      detail: '只埋三件事：生成成功率、从生成到保存的转化、导出次数。',
    }),
    node('review-plan', '排期与取舍', {
      detail: '先保可演示，再保可维护；诊断面板和知识库只读入口留作余量项。',
    }),
  ],
}));

export const DEMO_PRESETS: DemoPreset[] = [
  {
    id: 'rust',
    label: 'Rust 学习路线',
    description: '6 条主线、3 层，叶子带说明，适合讲层级与正文两层结构。',
    direction: 'right',
    content: RUST,
  },
  {
    id: 'rag',
    label: 'RAG 方案选型',
    description: '讲取舍与依据，节点里有"我的备注"，适合回答"凭什么这么选"。',
    direction: 'right',
    content: RAG,
  },
  {
    id: 'review',
    label: '一次产品评审',
    description: '5 条并列分支，适合演示双向布局与左右镜像。',
    direction: 'both',
    content: REVIEW,
  },
];
