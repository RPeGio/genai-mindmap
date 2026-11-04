import type { MindMapContent, MindNode } from '../api/types';
import type { Direction } from '../canvas/layout/tidy';

/**
 * 开发期示例：用来肉眼检查四种布局方向与文本截断，不经过后端。
 * 只在 `import.meta.env.DEV` 下被加载，生产构建里不会进主包。
 */
export interface SampleMap {
  id: string;
  label: string;
  direction: Direction;
  content: MindMapContent;
}

function mlTree(): MindNode {
  return {
    id: 'ml',
    label: '机器学习',
    children: [
      {
        id: 'ml-sup',
        label: '监督学习',
        children: [
          {
            id: 'ml-sup-cls',
            label: '分类',
            note: '分类：输出离散标签。\n常见任务：垃圾邮件识别、图像分类、疾病诊断。\n常用指标：准确率、精确率、召回率、F1。',
            children: [
              { id: 'ml-sup-cls-lr', label: '逻辑回归', children: [] },
              { id: 'ml-sup-cls-svm', label: '支持向量机', children: [] },
            ],
          },
          {
            id: 'ml-sup-reg',
            label: '线性回归与正则化（L1/L2，含特征缩放与共线性处理，这句故意超过二十四个字）',
            children: [],
          },
        ],
      },
      {
        id: 'ml-unsup',
        label: '无监督学习',
        children: [
          { id: 'ml-unsup-clu', label: '聚类', children: [] },
          { id: 'ml-unsup-dim', label: '降维', children: [] },
        ],
      },
      {
        id: 'ml-rl',
        label: '强化学习',
        children: [
          { id: 'ml-rl-pg', label: '策略梯度', children: [] },
          { id: 'ml-rl-vf', label: '价值函数', children: [] },
        ],
      },
    ],
  };
}

function thesisTree(): MindNode {
  return {
    id: 'thesis',
    label: '毕业论文写作',
    children: [
      {
        id: 'thesis-topic',
        label: '选题与定位',
        children: [
          { id: 'thesis-topic-gap', label: '找研究空白', children: [] },
          { id: 'thesis-topic-scope', label: '收窄范围', children: [] },
        ],
      },
      {
        id: 'thesis-survey',
        label: '文献综述',
        children: [
          { id: 'thesis-survey-search', label: '检索与筛选', children: [] },
          { id: 'thesis-survey-map', label: '画脉络图', children: [] },
        ],
      },
      {
        id: 'thesis-exp',
        label: '实验',
        children: [
          { id: 'thesis-exp-data', label: '数据集', children: [] },
          { id: 'thesis-exp-base', label: '基线对比', children: [] },
          { id: 'thesis-exp-abl', label: '消融实验', children: [] },
        ],
      },
      {
        id: 'thesis-write',
        label: '成文与答辩',
        children: [
          { id: 'thesis-write-draft', label: '初稿', children: [] },
          { id: 'thesis-write-slides', label: '讲稿与幻灯片', children: [] },
        ],
      },
    ],
  };
}

function reviewTree(): MindNode {
  return {
    id: 'review',
    label: '一次产品评审',
    children: [
      {
        id: 'review-goal',
        label: '目标',
        children: [{ id: 'review-goal-metric', label: '北极星指标', children: [] }],
      },
      {
        id: 'review-plan',
        label: '方案',
        children: [
          { id: 'review-plan-a', label: '方案 A：只读画布', children: [] },
          { id: 'review-plan-b', label: '方案 B：可编辑画布', children: [] },
        ],
      },
      {
        id: 'review-risk',
        label: '风险',
        children: [
          { id: 'review-risk-budget', label: '小内存机器扛不住', children: [] },
          { id: 'review-risk-key', label: '模型密钥外泄', children: [] },
        ],
      },
      { id: 'review-schedule', label: '排期', children: [] },
      { id: 'review-decide', label: '结论', children: [] },
    ],
  };
}

export const SAMPLE_MAPS: SampleMap[] = [
  {
    id: 'single',
    label: '单节点',
    direction: 'right',
    content: {
      title: '单个节点：这句话超过二十四个字时节点上应该出现省略号',
      root: {
        id: 'single',
        label: '单个节点：这句话超过二十四个字时节点上应该出现省略号，悬停卡片和右侧面板要能看到全文',
        children: [],
      },
    },
  },
  {
    id: 'right',
    label: '向右',
    direction: 'right',
    content: { title: '机器学习的主要方向', root: mlTree() },
  },
  {
    id: 'left',
    label: '向左',
    direction: 'left',
    content: { title: '机器学习的主要方向（与向右对照，应当左右镜像）', root: mlTree() },
  },
  {
    id: 'down',
    label: '向下',
    direction: 'down',
    content: { title: '毕业论文写作', root: thesisTree() },
  },
  {
    id: 'both',
    label: '双向',
    direction: 'both',
    content: { title: '一次产品评审', root: reviewTree() },
  },
];
