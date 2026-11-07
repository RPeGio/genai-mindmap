import { describe, expect, it } from 'vitest';

import type { Source, SourceRef, SourceUsage } from '../src/api/types';
import { excerptOf, locateInText, refName, refWhere, resolveRef, resolveRefs, windowOf } from '../src/mindmap/refs';

const TEXT = '矩阵的秩等于行向量组的极大线性无关组的个数。初等变换不改变矩阵的秩。';

function source(id = 's1', text: string | null = TEXT, kind: Source['kind'] = 'document'): Source {
  return {
    id,
    kind,
    filename: kind === 'document' ? '线代第一章.pdf' : null,
    text,
    span: null,
    status: 'parsed',
    error: null,
    parsed_pages: null,
    truncated: false,
    failed_pages: [],
  };
}

const usage: SourceUsage[] = [
  { source_id: 's1', kind: 'document', filename: '线代第一章.pdf', char_count: TEXT.length },
];

describe('定位引用', () => {
  it('有字符区间时直接采用（区间按码点算）', () => {
    const ref: SourceRef = { source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' };
    expect(locateInText(TEXT, ref)).toEqual({ start: 0, end: 6 });
  });

  it('区间越界时退回到按引文查找', () => {
    const ref: SourceRef = { source_id: 's1', start: 999, end: 1000, quote: '初等变换' };
    expect(locateInText(TEXT, ref)).toEqual({ start: 22, end: 26 });
  });

  it('没有区间也没有引文时定位不到', () => {
    expect(locateInText(TEXT, { source_id: 's1' })).toBeNull();
  });

  it('引文找不到时定位不到，而不是给个假位置', () => {
    expect(locateInText(TEXT, { source_id: 's1', quote: '这句话原文里没有' })).toBeNull();
  });

  it('码点与 UTF-16 下标不一致时按码点切，不会把 emoji 切坏', () => {
    const emoji = '😀😀结论：秩相等';
    const ref: SourceRef = { source_id: 's2', quote: '结论' };
    // 😀 占两个 UTF-16 单元，码点下标是 2 而不是 4
    expect(locateInText(emoji, ref)).toEqual({ start: 2, end: 4 });
  });

  it('空白被压过的引文仍能定位', () => {
    const text = '第一行\n\n  第二行 有关键词';
    const ref: SourceRef = { source_id: 's3', quote: '第二行 有关键词' };
    expect(locateInText(text, ref)).not.toBeNull();
  });
});

describe('位置说明与来源名', () => {
  it('页码与字符区间同时存在时都写', () => {
    expect(refWhere({ source_id: 's1', page: 3, start: 119, end: 168 })).toBe('第 3 页 · 第 120–168 字');
  });

  it('只有页码', () => {
    expect(refWhere({ source_id: 's1', page: 5 })).toBe('第 5 页');
  });

  it('两项都没有时返回空串', () => {
    expect(refWhere({ source_id: 's1' })).toBe('');
  });

  it('来源名优先用文件名，缺失时退回类型名', () => {
    expect(refName({ source_id: 's1' }, usage)).toBe('文档 · 线代第一章.pdf');
    expect(refName({ source_id: 's9' }, [])).toBe('来源 s9');
  });
});

describe('解析成可绘制形状', () => {
  it('有原文时给出摘录与高亮区间', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [source()], usage);
    expect(resolved.source).not.toBeNull();
    expect(resolved.quote).toBe('矩阵的秩等于');
    expect(resolved.start).toBe(0);
    expect(resolved.end).toBe(6);
    expect(resolved.excerpt).toContain('矩阵的秩等于');
    expect(resolved.where).toBe('第 1–6 字');
  });

  it('没有原文（打开已保存导图）时仍显示摘录，但不给高亮区间', () => {
    const resolved = resolveRef({ source_id: 's1', page: 2, quote: '初等变换' }, [], usage);
    expect(resolved.source).toBeNull();
    expect(resolved.quote).toBe('初等变换');
    expect(resolved.excerpt).toBe('');
    expect(resolved.start).toBeNull();
    expect(resolved.where).toBe('第 2 页');
  });

  it('没有 quote 但有字符区间时，从原文切出摘录', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 4 }, [source()], usage);
    expect(resolved.quote).toBe('矩阵的秩');
  });

  it('原文和摘录都没有时返回空串，不抛错', () => {
    const resolved = resolveRef({ source_id: 's1' }, [source('s1', null)], usage);
    expect(resolved.quote).toBe('');
    expect(resolved.excerpt).toBe('');
  });

  it('resolveRefs 接受 undefined 并保持顺序', () => {
    expect(resolveRefs(undefined, [], [])).toEqual([]);
    const list = resolveRefs(
      [{ source_id: 's1', quote: '甲' }, { source_id: 's1', quote: '乙' }],
      [source()],
      usage,
    );
    expect(list.map(item => item.quote)).toEqual(['甲', '乙']);
  });

  it('片段截断处加省略号', () => {
    const long = '甲'.repeat(300);
    const text = excerptOf(long, 150, 160, 10);
    expect(text.startsWith('…')).toBe(true);
    expect(text.endsWith('…')).toBe(true);
    expect(Array.from(text).length).toBe(32);
  });
});

describe('原文片段的三段切分', () => {
  it('命中部分单独成段，前后各自带上下文', () => {
    const { before, hit, after } = windowOf(TEXT, 22, 26, 4);
    expect(hit).toBe('初等变换');
    expect(before).toBe('…的个数。');
    expect(after).toBe('不改变矩…');
  });

  it('三段拼起来等于整段摘录，不会有字被吃掉', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [source()], usage);
    expect(`${resolved.before}${resolved.hit}${resolved.after}`).toBe(resolved.excerpt);
    expect(resolved.hit).toBe('矩阵的秩等于');
  });
});