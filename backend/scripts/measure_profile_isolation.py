"""按导图隔离的画像方向：**两张不同主题的图**上的跨图污染实测。

## 之前那版 A/B 测不出这件事

`measure_profile_ab.py --ab` 是**同一主题跑三次**、A 组关画像 / B 组开画像。
它能量"画像有没有改变结构"，但它量不了跨图污染——因为它的两组用的是同一批
材料，**根本没有第二张图**。这次要测的那个数（"图 B 的命中里有几条是图 A 的
方向顶上来的"）在那套设计里恒为 0，不是因为没问题，是因为没测。

## 这一版怎么测

两张图，各 3 份材料，主题互不重叠：

```
图 A  「机器学习的主要方向」   ML_A1 / ML_A2 / ML_A3
图 B  「碳中和政策」           CN_B1 / CN_B2 / CN_B3
```

两张图都用 `library=True` 检索——这是关键，也是污染**唯一**能发生的路径：
候选池 = 本次请求的材料 + 库里挑出的 `LIBRARY_PICK=5` 份。图 B 检索时库里
剩下的正好是图 A 的 3 份材料，于是"图 A 的材料"确实进了图 B 的候选池。
（`_library_sources` 的注释写着"覆盖率全为 0 时照样取前 LIBRARY_PICK 份"，
所以主题完全不沾也照样进。）

两组唯一的差别是**方向从哪来**：

  - `global`（改动之前的行为）：`profile.build_directions(profile._samples())`
    ——库里全部材料与导图，跨图平均。图 B 的检索会被图 A 的方向加权；
  - `isolated`（改动之后）：`profile.build_directions_from_sources(CN_B1..3)`
    ——只有图 B 自己的材料。图 A 的方向不存在于这个列表里。

三个指标：

  1. **跨图污染**：图 B 的 `top_k` 命中里，`source_id` 属于图 A 材料的有几条；
  2. **本图贴合**：命中里属于图 B 自己材料的有几条；
  3. **成本**：`embed_texts` 被调用了几次（含批次数）。隔离那一组应该是 0
     ——方向只读 `chunk_vectors` 里现成的块向量。

## 真跑

```
python scripts/measure_profile_isolation.py --run
```

要 `MINDMAP_EMBEDDING_API_KEY`（或 `DASHSCOPE_API_KEY`）+ `MINDMAP_EMBEDDING_BASE_URL`。
**走临时 database**（每次现建、跑完删掉），不动 `MINDMAP_PG_DSN` 指的那个库：这个脚本
会反复清库、重灌材料，跑在真库上等于把用户的 18 份来源与 23 张图冲掉。要看结果就
`--keep-db`（保留这个库并打印 psql 连法），要跑在固定的库上就 `--db <库名>`。

不调模型：量的是检索 + 重排，`rag_query` 那一层不需要 LLM 参与就能给出这三个数。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)
# `scripts/` 不是包，`_pg_tmp` 就在旁边；显式插一次（与本目录其它脚本同一做法）。
sys.path.insert(0, SCRIPTS)

import _pg_tmp  # noqa: E402  （必须在 sys.path 插好之后）

# ---------------------------------------------------------------- 两组材料
#
# 手写而不是从库里取：库里 ML 材料有 6 份、碳中和只有 2 份（每份 2 个块），
# 达不到 `MIN_SAMPLES=5`。凑不齐就没法把两张图放到同一张表上比。
# 手写还能保证两组的**词汇完全不重叠**——词表重叠会让"污染"变成"字面相关"，
# 那样测出来的数说清不了一件事。

ML_MATERIALS = [
    ("ml-监督学习与模型评估.txt", """
机器学习的主流派别按监督信号分成三类。监督学习给定带标签的样本，模型学习从输入到
标签的映射，常见任务包括分类与回归。分类问题的目标是预测离散类别，例如判断一封邮件
是否为垃圾邮件、判断一张影像里是否含病灶；回归问题的目标是预测连续数值，例如房价、
气温、设备剩余寿命。监督学习的两条经典基线是线性回归与逻辑回归，它们可解释、训练快，
在样本量不大、特征工程成熟的场景里往往比深度模型更稳。评估监督模型的核心是把数据
切成训练集、验证集与测试集，训练集拟合参数，验证集选超参数，测试集只用来报最终指标，
三者混用会让报出来的数字虚高。分类任务的常用指标有准确率、精确率、召回率与 F1，
准确率在不平衡数据上会骗人：九千条负样本、一千条正样本时，全判负也有九成准确率，
但召回率为零。这时要看精确率与召回率的权衡，以及 PR 曲线下的面积。回归任务常用
均方误差、平均绝对误差与决定系数，均方误差对大偏差敏感，平均绝对误差对离群点更稳。
过拟合是监督学习最主要的风险：模型在训练集上误差很低、在测试集上误差很高，说明它
记住了噪声而不是规律。对策包括增加数据、正则化、早停、交叉验证与降低模型容量。
欠拟合相反，训练与测试误差都高，通常要加特征、加容量或减少正则。交叉验证把数据分成
K 份轮流做验证，能在样本有限时给出更可靠的泛化估计，K 常取五或十。
""".strip()),
    ("ml-无监督与降维聚类.txt", """
无监督学习不依赖标签，目标是在数据自身结构里找规律。最常见的两件事是聚类与降维。
聚类把样本分成若干组，使组内相似、组间相异。K 均值是最常用的聚类算法：先选 K 个
初始中心，把每个样本分给最近的中心，再用每组的均值更新中心，迭代到中心不再移动。
它快、易实现，但要求事先给 K，且对初始中心与离群点敏感，所以实践中常用 K 均值加加加
（k-means++）做初始化，用肘部法或轮廓系数选 K。层次聚类不需要给 K，它按距离不断合并
或分裂，输出一棵树，切在哪一层就得到几个簇；代价是复杂度高，样本上万时基本不可用。
密度聚类（DBSCAN）按"邻域内样本数超过阈值"把样本连成簇，能识别任意形状的簇并把
噪声点单独标出来，但它对两个参数（半径与最小样本数）敏感，密度差异大的数据上效果差。
降维解决的是"维度高、样本少、距离失效"三件事。主成分分析把数据投影到方差最大的
若干正交方向上，用少数几个主成分解释大部分方差，它线性、可逆、有闭式解，是降维的
默认基线。t-SNE 与 UMAP 是非线性降维，擅长把高维数据的局部邻域结构画到二维平面上，
适合可视化，但不保持全局距离，两簇之间的远近不能用它来解读。降维之后再做聚类或
分类是常见组合，但要小心：降维丢掉的方差里可能正好是判别信息，评估时必须回到原始
空间或用同一套变换处理训练与测试。
""".strip()),
    ("ml-神经网络与训练.txt", """
深度学习的核心是多层神经网络的表示学习。一个全连接层做的是线性变换加非线性激活，
堆叠多层就能逼近复杂的非线性函数。反向传播用链式法则把损失对每个参数的梯度算出来，
优化器据此更新参数。随机梯度下降每次只用一小批样本估计梯度，噪声带来正则效果，
也让训练能在海量数据上跑起来。动量、RMSProp 与 Adam 是三种常见的改进：动量累积
历史梯度方向以穿过平坦区域，RMSProp 按梯度平方的滑动平均缩放每个参数的学习率，
Adam 两者都做，通常不需要精细调学习率，但它在某些任务上泛化略差于带动量的 SGD。
激活函数从 sigmoid 换到 ReLU 解决了深层网络的梯度消失：sigmoid 在两端饱和、导数接近零，
误差传几层就消失了；ReLU 在正区间导数为常数，训练更快，代价是负区间导数为零，
于是有了 Leaky ReLU、ELU 与 GELU 这些变体。批归一化把每层的输入重新标准化，
允许更大的学习率、降低对初始化的敏感，是深层网络能训起来的关键工程手段之一。
卷积网络利用局部连接与权重共享处理图像：卷积核在空间上滑动，池化降采样，
感受野逐层扩大；残差连接让梯度有直连通道，使上百层的网络可训练。
循环网络处理序列，用隐藏状态携带历史信息，长短期记忆网络用门控缓解长程依赖的
梯度衰减；Transformer 换成自注意力，一次看到全序列、可并行，成了当下序列建模的
主力。训练侧的实务包括：学习率预热与余弦退火、梯度裁剪、混合精度、数据增强、
早停与模型集成。过拟合在深度网络里尤其常见，Dropout 随机丢弃神经元、权重衰减
限制参数规模、标签平滑软化目标分布，都是常用手段。
""".strip()),
]

CN_MATERIALS = [
    ("cn-双碳目标与政策体系.txt", """
碳中和指在一定时期内，人为排放的二氧化碳与人为移除的二氧化碳相抵消，净排放为零。
我国的双碳目标是二零三零年前碳达峰、二零六零年前碳中和。政策体系按"1+N"搭建：
"1"是顶层设计文件，"N"是分领域分行业的实施方案与保障政策。顶层设计明确了五个方面：
能源结构调整、产业结构优化、重点行业节能降碳、生态碳汇提升、技术创新与制度保障。
能源侧的核心是把化石能源消费逐步压下来、把非化石能源比重提上去，重点是风电与光伏的
规模化开发、水电与核电的稳定支撑、以及新型电力系统的建设。新型电力系统要解决
新能源的间歇性与波动性，靠的是储能、需求侧响应、跨区输电与灵活调节电源。
工业侧的重点行业包括钢铁、水泥、有色、石化化工与建材，路径是产能置换、能效提升、
原料替代、废钢与再生资源循环，以及以氢代碳的工艺改造。交通侧推进新能源汽车、
公转铁与公转水、绿色港口与船舶岸电。建筑侧抓新建建筑的绿色标准、既有建筑的节能改造、
以及供暖系统的电气化与热泵化。碳汇侧靠森林、草原、湿地与海洋的固碳能力提升，
同时严控土地利用变化带来的排放。制度侧包括碳排放统计核算、能耗双控转向碳排放双控、
全国碳市场扩容、绿色金融与碳定价、以及气候投融资。
""".strip()),
    ("cn-碳市场与碳核算.txt", """
全国碳排放权交易市场是碳定价的主要抓手。它的基本结构是配额总量加交易：主管部门
按行业基准给重点排放单位分配配额，配额不足的企业在市场上买，富余的可以卖。
配额分配从免费分配起步，逐步提高有偿分配比例，是"先立后破"的稳妥做法。
市场的价格信号要有效，需要三件事同时成立：数据真实、配额稀缺、违规成本足够高。
数据真实靠核算与核查：企业按行业核算指南报告排放量，第三方机构核查，主管部门
抽查执法。核算的边界问题最容易出错：直接排放与间接排放要分开，外购电力与热力的
排放归到消费侧，避免重复计算。碳市场的覆盖范围从发电行业起步，逐步纳入钢铁、
建材、有色、石化、化工、造纸与航空。扩容的难点在于产品结构复杂、基准值难统一、
数据基础薄弱。除配额市场之外，自愿减排交易市场（CCER）为林业碳汇、甲烷利用、
可再生能源等项目提供额外的减排量供给，但它的项目质量参差，需要严格的额外性论证
与监测方法学。国际上，欧盟碳边境调节机制对进口的高碳产品征收碳成本差额，
这使出口企业面临"碳成本转移"的压力，也推动国内核算体系与国际标准对接。
碳足迹核算要区分组织层级与产品层级：组织层级看企业边界内的排放，
产品层级看全生命周期，从原料获取、生产、运输、使用到废弃，边界不同结果可能差几倍。
""".strip()),
    ("cn-低碳技术与转型路径.txt", """
低碳技术按成熟度可以分成三类。第一类已商业化，主要是能效提升与可再生能源发电：
高效电机、余热余压回收、工业锅炉改造、光伏与风电。它们的减排量大、成本可控，
是近期减排的主力。第二类在示范与推广阶段：长时储能、氢冶金、碳捕集利用与封存、
生物质掺烧与绿色甲醇。它们的成本还高，需要政策补贴与规模化降本。第三类是前沿探索：
直接空气捕集、可控核聚变、以及基于矿化的永久固碳。转型路径的时间表决定了技术
路线的优先级：达峰前的重点是控增量、压强度，靠能效与能源结构；达峰后的重点是
减存量，靠工艺替代与碳捕集。电力系统的脱碳是整个转型的中枢，因为终端用能
电气化之后，电的清洁程度决定了全社会的排放。储能是新型电力系统的关键环节，
按持续时间分为小时级、日级与季节级，抽水蓄能、电化学储能、压缩空气与氢储能
各占一段。氢能的角色要分清：绿氢来自可再生电力电解水，是真正的零碳原料；
灰氢来自化石燃料重整，减排效果有限；蓝氢在灰氢基础上加碳捕集，仍有甲烷泄漏风险。
钢铁行业的深度脱碳路线有三条：高炉喷吹富氢、氢基直接还原、以及电炉短流程加废钢，
后者受废钢资源量限制。水泥的排放七成来自石灰石分解的工艺排放，靠燃料替代解决不了，
必须走碳捕集或原料替代。转型的社会维度同样重要：重点行业的就业结构会变，
需要转岗培训与区域转型支持，公正转型不是附加题。
""".strip()),
]


@dataclass
class Arm:
    """一组实验的结果。"""

    name: str
    pool: str
    directions: int
    hits: int
    cross: int          # 候选里属于**另一张图**材料的条数（检索层带进来的）
    own: int            # 候选里属于本图材料的条数
    cross_bonus: float  # **跨图命中拿到的画像加分总额**（画像层污染，最干净的那个数）
    own_bonus: float    # 本图命中拿到的画像加分总额
    cross_top1: bool    # 跨图命中是不是被顶到了第 1 名
    embed_calls: int    # `embed_texts` 被调用次数（批次数）
    embed_texts: int    # 送进去的文本条数
    lifted: int         # 名次上升的命中条数
    order_before: str
    order_after: str


class Counter:
    """数 `embed_texts` 的调用。**不改被测代码**：只替换模块级符号。

    两处都要换：`app.knowledge.service` 用 `from .embedding import embed_texts`
    （自己的模块级符号），`app.prefs.profile` 用 `embedding.embed_texts`（属性访问）。
    漏一处就会漏掉一半成本，而"成本"正是这一节要回答的三件事之一。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.texts = 0
        self._saved: list[tuple[Any, str, Any]] = []

    def install(self) -> None:
        from app.knowledge import embedding, service
        from app.prefs import profile

        real = embedding.embed_texts

        def wrapped(texts: list[str]) -> Any:
            self.calls += 1
            self.texts += len(texts)
            return real(texts)

        # service 的符号是它自己模块里的；profile 走 `embedding.embed_texts`。
        for module, attribute in ((service, "embed_texts"), (embedding, "embed_texts")):
            self._saved.append((module, attribute, getattr(module, attribute)))
            setattr(module, attribute, wrapped)
        assert profile.embedding is embedding, "profile 的 embedding 必须是同一个模块"

    def remove(self) -> None:
        for module, attribute, original in self._saved:
            setattr(module, attribute, original)
        self._saved.clear()


def _prepare_db() -> list[str]:
    """灌两组材料、返回全部 source_id（前 3 个是图 A、后 3 个是图 B）。

    材料进库走 `library.ingest`——与生产路径同一个入口，所以落库形态、`content_hash`
    唯一约束、切块台账全都与真跑一致。

    库由调用方先准备好（`_run` 里建临时 database 并 `init_schema()`）：这个函数自己
    不碰 DSN、也不建表，否则"谁负责清理"会分散到两处。
    """
    from app.contracts.source import Source
    from app.knowledge import library

    ids: list[str] = []
    for filename, text in (*ML_MATERIALS, *CN_MATERIALS):
        source = Source(id=filename, filename=filename, kind="text", text=text)
        ids.append(library.ingest(source))
    return ids


def _warm(ids: list[str], counter: Counter) -> None:
    """把两组材料的子块向量灌进 `chunk_vectors`——模拟"这些材料被检索过一次"。

    这是 A/B **都要付**的底座成本，所以它不计入任何一组的画像成本；而它产生的
    块向量正是隔离那一组"零往返"的全部来源。真实链路里这笔钱由
    `search_parent` 付（`_vector_scores` 第一次遇到这些块时 embed 并写缓存）。
    """
    from app.knowledge import embedding, vector_cache
    from app.knowledge.library import load
    from app.knowledge.service import _chunk_tree

    model = embedding.model_name()
    for source_id in ids:
        source = load(source_id)
        if source is None:
            continue
        text = source.text
        fresh: list[tuple[str, list[float]]] = []
        for node in _chunk_tree(text):
            if node.level != 1:
                continue
            quote = text[node.start:node.end]
            key = vector_cache.text_hash(quote)
            if vector_cache.get_vectors([key], model):
                continue
            fresh.append((key, quote))
        for start in range(0, len(fresh), 10):
            window = fresh[start:start + 10]
            # `embed_texts` 在 `Counter.install()` 之后已经是计数版，所以这里数到的
            # 就是真实批次。用模块属性访问而不是 `from ... import`：后者会把
            # 安装前的原函数抓进局部变量，绕开计数器。
            vectors = embedding.embed_texts([quote for _, quote in window])
            vector_cache.put_vectors(
                [(key, vector) for (key, _), vector in zip(window, vectors) if vector],
                model)


def _hits_summary(hits: list[Any], own_ids: set[str], other_ids: set[str]) -> tuple[int, int, str]:
    """(跨图条数, 本图条数, 顺序记号)。"""
    cross = sum(1 for hit in hits if hit.source_id in other_ids)
    own = sum(1 for hit in hits if hit.source_id in own_ids)
    order = "".join(
        "A" if hit.source_id in other_ids else ("B" if hit.source_id in own_ids else "?")
        for hit in hits)
    return cross, own, order


def _bonus_split(hits: list[Any], bonuses: dict[int, float],
                 own_ids: set[str], other_ids: set[str]) -> tuple[float, float]:
    """(跨图命中拿到的加分, 本图命中拿到的加分)。

    **这是"画像层污染"唯一干净的量化。** 不数条数：`boost` 的契约是不增不减
    （约束 1），跨图命中的条数在加权前后恒等，数它只会得到"两组一样"。
    也不数名次改善：名次会互相挤压——别的条目被降下去，一分没加的那条也会相对
    上升。只有"它自己拿到了几分"能归因。
    """
    cross = sum(bonus for index, bonus in bonuses.items()
                if hits[index].source_id in other_ids)
    own = sum(bonus for index, bonus in bonuses.items()
              if hits[index].source_id in own_ids)
    return round(cross, 4), round(own, 4)


def _rank_of(hits: list[Any], hit: Any) -> int:
    for rank, item in enumerate(hits):
        if item is hit:
            return rank
    return -1


def _detail(hits: list[Any], boosted: Any, own_ids: set[str],
            other_ids: set[str], titles: dict[str, str]) -> list[str]:
    """逐条明细：谁、与最佳方向的余弦、拿到几分、名次怎么动。

    **这张表是这一节唯一能验证结论的东西。** 汇总数字会说谎，因为 `boost` 的
    契约就是"不增不减"（约束 1）——它不可能改变 `top_k` 里有几条跨图命中。
    能变的只有**名次**与**加分**，而"跨图命中拿到几分、被顶到第几名"才是
    用户感受到的那种污染。
    """
    lines: list[str] = []
    for index, hit in enumerate(hits):
        before = index
        after = _rank_of(boosted.hits, hit)
        cosine = boosted.cosines.get(index)
        bonus = boosted.bonuses.get(index, 0.0)
        if hit.source_id in own_ids:
            mark = "B 本图"
        elif hit.source_id in other_ids:
            mark = "A 另一张图"
        else:
            mark = "? 库里其它"
        arrow = f"{before + 1}→{after + 1}"
        move = "  ↑上移" if after < before else ("  ↓下移" if after > before else "")
        lines.append(
            f"    {mark:<10}{titles.get(hit.source_id, hit.source_id)[:24]:<26}"
            f"cos {cosine if cosine is not None else float('nan'):.3f}  "
            f"加分 {bonus:.4f}  名次 {arrow}{move}")
    return lines


def _run(keep_db: bool = False, db_spec: str = "") -> int:
    from app.knowledge import embedding, service
    from app.knowledge.library import load
    from app.prefs import profile

    if not embedding.configured():
        print("向量服务未配置：设置 MINDMAP_EMBEDDING_API_KEY 与 "
              "MINDMAP_EMBEDDING_BASE_URL 再跑。")
        return 2

    # 库的生命周期在这里收口：`--db` 给了就指向那个库（同集群里没有就建），
    # 没给就现建一个一次性 database——两种都由 `_pg_tmp` 负责 `init_schema()`。
    # 为什么不用 `with` 包住整个 `_run`：`--keep-db` 要"跑完**不**删"，而 `with` 的
    # 出口无条件删；所以这里显式 create，清理放在 finally 里按 keep_db 分叉。
    if db_spec:
        _dsn, created = _pg_tmp.persistent_database(db_spec)
        temp: _pg_tmp.TempDatabase | None = None
        db_label = db_spec + ("（本次新建）" if created else "（复用已有）")
    else:
        temp = _pg_tmp.TempDatabase.create("mm_profile_iso")
        db_label = temp.name
    print(f"库：{db_label}")

    counter = Counter()
    counter.install()
    try:
        # 灌材料放在 try 里：建库已经成功，这一步要是失败，库里会留下一个
        # 孤儿 database（临时库没人收、--keep-db 之外也不该留），所以清理统一走 finally。
        ids = _prepare_db()
        a_ids, b_ids = set(ids[:3]), set(ids[3:])
        a_sources = [source for source in (load(i) for i in ids[:3]) if source]
        b_sources = [source for source in (load(i) for i in ids[3:]) if source]
        titles = {source.id: (source.filename or source.id)
                  for source in (*a_sources, *b_sources)}
        _warm(ids, counter)
        print(f"底座：{len(ids)} 份材料入库，块向量预热用掉 {counter.calls} 次调用 / "
              f"{counter.texts} 条文本（A/B 共用，不计入任何一组的画像成本）\n")

        # 两张图各跑两组。图 B 那一行是主测：它问的是"图 B 的检索里会不会冒出
        # 图 A 的命中"；图 A 那一行是镜像，用来确认结论不是单向的。
        arms: list[Arm] = []
        for label, own_sources, own_ids, other_ids, question in (
            ("图 B「碳中和政策」← 防 ML 串味", b_sources, b_ids, a_ids, "碳中和政策"),
            ("图 A「机器学习的主要方向」← 防碳中和串味", a_sources, a_ids, b_ids,
             "机器学习的主要方向"),
        ):
            print("=" * 78)
            print(label)
            print("=" * 78)
            for mode in ("global", "isolated"):
                if mode == "global":
                    # 改动之前：方向从**库里全部**样本算（跨图平均）。
                    counter.calls = counter.texts = 0
                    directions = profile.build_directions(profile._samples())
                    pool = "（全局，隔离之前）"
                else:
                    # 改动之后：方向**只从这张图的材料**算，且只读块向量缓存。
                    counter.calls = counter.texts = 0
                    directions = profile.build_directions_from_sources(own_sources)
                    pool = profile.pool_key(run_id="measure")
                direction_calls, direction_texts = counter.calls, counter.texts

                counter.calls = counter.texts = 0
                hits = service.search_parent(
                    own_sources, question, 5, library=True)
                search_calls, search_texts = counter.calls, counter.texts

                cross_before, own_before, order_before = _hits_summary(
                    hits, own_ids, other_ids)
                boosted = profile.boost(hits, directions=directions)
                cross_after, own_after, order_after = _hits_summary(
                    boosted.hits, own_ids, other_ids)
                cross_bonus, own_bonus = _bonus_split(
                    hits, boosted.bonuses, own_ids, other_ids)
                top1 = boosted.hits[0] if boosted.hits else None
                cross_top1 = bool(top1 is not None and top1.source_id in other_ids)

                arms.append(Arm(
                    name=f"{label.split('「')[1].split('」')[0]} / {mode}",
                    pool=pool, directions=len(directions), hits=len(boosted.hits),
                    cross=cross_before, own=own_before,
                    cross_bonus=cross_bonus, own_bonus=own_bonus,
                    cross_top1=cross_top1,
                    embed_calls=direction_calls + search_calls,
                    embed_texts=direction_texts + search_texts,
                    lifted=len(boosted.lifted),
                    order_before=order_before, order_after=order_after))
                print(f"\n[{mode}] 方向 {len(directions)} 个（池 {pool}）"
                      f"  未加权原因：{boosted.reason or '无（正常加权）'}")
                print(f"  画像方向成本：{direction_calls} 次调用 / {direction_texts} 条文本")
                print(f"  检索成本：    {search_calls} 次调用 / {search_texts} 条文本")
                print(f"  候选：{order_before}  （跨图 {cross_before} / 本图 {own_before}）"
                      f"  → 重排后 {order_after}")
                print(f"  画像加分：跨图 {cross_bonus:.4f} / 本图 {own_bonus:.4f}"
                      f"    跨图命中登顶：{'是' if cross_top1 else '否'}")
                for line in _detail(hits, boosted, own_ids, other_ids, titles):
                    print(line)
                for item in directions[:5]:
                    print(f"    方向 {item.label[:34]:<36}权重 {item.weight:.2f} "
                          f"样本 {item.samples}")
            print()

        print("=" * 78)
        print(f"{'组':<30}{'方向':>4}{'跨图':>5}{'本图':>5}{'跨图加分':>9}"
              f"{'本图加分':>9}{'登顶':>5}{'调用':>5}{'文本':>5}  候选→重排")
        print("-" * 78)
        for arm in arms:
            print(f"{arm.name:<30}{arm.directions:>4}{arm.cross:>5}{arm.own:>5}"
                  f"{arm.cross_bonus:>9.4f}{arm.own_bonus:>9.4f}"
                  f"{('是' if arm.cross_top1 else '否'):>5}"
                  f"{arm.embed_calls:>5}{arm.embed_texts:>5}"
                  f"  {arm.order_before}→{arm.order_after}")
        print("-" * 78)
        print("记号：A = 另一张图的材料，B = 本图材料，? = 库里其它来源")
        print("跨图/本图 = 候选里各有多少条（由检索决定，`boost` 不增不减，两组必然相同）")
        print("跨图加分 = 跨图命中**实际拿到的画像加分**（画像层污染，越低越好，0 = 完全没被碰过）")
        print("本图加分 = 本图命中拿到的画像加分（越高越好，>0 表示个性化确实在起作用）")
        print("\n注意：本临时库只有 6 份材料，全局那组的 `_samples()` 因此只给 6 条")
        print("样本 = 1 次调用。真库（18 份来源 / 6 张够结点的导图 = 24 条样本）")
        print("是 3 次调用（24/10 向上取整）；材料与图再多会顶到 MAX_SAMPLES=60")
        print("条上限 = 6 次调用。隔离那组在两种库里都是 0 次。")
    finally:
        counter.remove()
        # 池永远要关（连接攥着不放，同进程后面改 DSN 会拿到指向旧库的池）；
        # 删不删库看 --keep-db：临时库默认删，--db 指的持久库一律不删。
        if temp is not None and not keep_db:
            temp.close()
        else:
            from app.db import pool as pool_module

            pool_module.close_pool()
            if temp is not None:
                print(f"\n临时 database 已保留：{temp.name}")
                print(f"连上去看：psql \"{temp.dsn}\"")
                print(f"用完删掉：DROP DATABASE \"{temp.name}\" WITH (FORCE);")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="真跑（需要 embedding 凭证）")
    parser.add_argument("--keep-db", action="store_true",
                        help="跑完保留临时库（打印库名与 psql 连法）")
    parser.add_argument("--db", default="",
                        help="持久库（库名或完整 DSN）；不给就现建一个临时库")
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    return _run(keep_db=args.keep_db, db_spec=args.db)


if __name__ == "__main__":
    raise SystemExit(main())
