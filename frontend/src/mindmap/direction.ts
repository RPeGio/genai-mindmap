import type { LayoutKind, MindMapContent, MindMapTheme } from '../api/types';
import type { Direction } from '../canvas/layout/tidy';

/**
 * `Direction`（界面上的四种朝向）与契约 v2 的 `layout` + `theme.direction` 互转。
 *
 * 两个字段是有分工的：`layout` 决定树"怎么排"（横向/纵向，将来还有时间轴等），
 * `theme.direction` 只描述横向布局朝哪边。界面把它们合成一个四选一控件，
 * 换算只在这一处发生，别的地方不要自己拼 `layout`。
 */
export function directionOf(content: MindMapContent | null | undefined): Direction {
  if (!content) return 'right';
  // 纵向布局与 theme.direction 无关，先看 layout。
  if (content.layout === 'vertical') return 'down';
  const direction = content.theme?.direction;
  return direction === 'left' || direction === 'both' ? direction : 'right';
}

/**
 * 把朝向写进 content，返回新对象（不改入参，撤销栈拿的是旧快照）。
 *
 * 注意 `layout` 只在 "tree / vertical / 缺省" 之间切换：将来若后端下发
 * `logic` / `timeline` / `manual`，这里不能把它们悄悄降级成 tree —— 那等于
 * 用一次"换方向"抹掉别人存下的布局类型。
 */
export function withDirection(content: MindMapContent, direction: Direction): MindMapContent {
  const current = content.layout;
  const horizontal = current === undefined || current === 'tree' || current === 'vertical';
  const layout: LayoutKind = direction === 'down' ? 'vertical' : horizontal ? 'tree' : current;

  const theme: MindMapTheme = {
    ...(content.theme ?? {}),
    // 纵向布局不读 direction，但要留一个确定值，免得"向下 → 向左"时读回旧值。
    direction: direction === 'down' ? 'right' : direction,
  };

  return { ...content, schema_version: 2, layout, theme };
}
