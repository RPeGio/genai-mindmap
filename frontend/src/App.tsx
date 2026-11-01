import { useEffect, useState } from 'react';
import { api } from './api';
import type { MindMapContent, MindMapRecord, MindNode } from './types';

function editTree(node: MindNode, id: string, change: (node: MindNode) => MindNode): MindNode {
  if (node.id === id) return change(node);
  return { ...node, children: node.children.map(child => editTree(child, id, change)) };
}
function removeNode(node: MindNode, id: string): MindNode {
  return { ...node, children: node.children.filter(child => child.id !== id).map(child => removeNode(child, id)) };
}
function NodeEditor({ node, root, onRename, onAdd, onRemove }: {
  node: MindNode; root?: boolean; onRename: (id: string, label: string) => void;
  onAdd: (id: string) => void; onRemove: (id: string) => void;
}) {
  return <li><div className="node-row">
    <input aria-label="节点名称" value={node.label} maxLength={200} onChange={e => onRename(node.id, e.target.value)} />
    <button type="button" onClick={() => onAdd(node.id)}>添加子节点</button>
    {!root && <button type="button" className="quiet" onClick={() => onRemove(node.id)}>删除</button>}
  </div>{node.children.length > 0 && <ul>{node.children.map(child => <NodeEditor key={child.id} node={child} onRename={onRename} onAdd={onAdd} onRemove={onRemove} />)}</ul>}</li>;
}

export default function App() {
  const [text, setText] = useState('');
  const [content, setContent] = useState<MindMapContent | null>(null);
  const [record, setRecord] = useState<MindMapRecord | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState<string | null>(null);
  const [maps, setMaps] = useState<MindMapRecord[]>([]);
  const [message, setMessage] = useState('默认是演示模式；配置模型后才会调用 AI。');
  const [busy, setBusy] = useState(false);
  const isDirty = content !== null && JSON.stringify(content) !== savedSnapshot;
  const refresh = async () => setMaps(await api.list());
  useEffect(() => { refresh().catch(() => setMessage('无法连接后端，请先启动后端服务。')); }, []);
  useEffect(() => {
    if (!isDirty) return;
    const warnOnLeave = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnOnLeave);
    return () => window.removeEventListener('beforeunload', warnOnLeave);
  }, [isDirty]);
  const canDiscard = () => !isDirty || window.confirm('当前导图有未保存的修改，继续将丢失这些修改。确定继续吗？');
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); } catch (error) { setMessage(error instanceof Error ? error.message : '操作失败'); }
    finally { setBusy(false); }
  };
  const generate = () => {
    if (!text.trim()) { setMessage('请输入主题或文本。'); return; }
    if (!canDiscard()) return;
    void run(async () => {
      const result = await api.generate(text);
      setContent(result.content); setRecord(null); setSavedSnapshot(null);
      setMessage(result.mode === 'demo' ? '当前结果是固定结构的演示数据，不是 AI 生成。' : '模型已生成导图，请检查内容后保存。');
    });
  };
  const save = () => run(async () => {
    if (!content) return;
    const saved = record ? await api.update(record, content) : await api.create(content);
    setRecord(saved); setContent(saved.content); setSavedSnapshot(JSON.stringify(saved.content));
    try { await refresh(); setMessage('已保存。'); }
    catch { setMessage('导图已保存，但列表刷新失败，请稍后重新打开页面。'); }
  });
  const open = (id: string) => {
    if (!canDiscard()) return;
    void run(async () => {
      const saved = await api.get(id);
      setRecord(saved); setContent(saved.content); setSavedSnapshot(JSON.stringify(saved.content)); setMessage('已打开导图。');
    });
  };
  const changeRoot = (fn: (root: MindNode) => MindNode) => setContent(current => current ? { ...current, root: fn(current.root) } : null);
  return <main><header><h1>生成式 AI 思维导图</h1><p>项目开发骨架 · 文本到可编辑导图</p></header>
    <div className="layout"><section><h2>创建导图</h2>
      <label htmlFor="prompt">主题或文本</label><textarea id="prompt" disabled={busy} value={text} maxLength={10000} onChange={e => setText(e.target.value)} placeholder="例如：整理机器学习的主要方向" />
      <button disabled={busy} onClick={generate}>生成导图</button>
      <p role="status" className="status">{message}</p>
      <h2>已保存的导图</h2>{maps.length === 0 && <p>暂无导图</p>}
      <ul className="saved">{maps.map(map => <li key={map.id}><button className="link" disabled={busy} onClick={() => open(map.id)}>{map.content.title}</button></li>)}</ul>
    </section><section><fieldset className="editor-fields" disabled={busy}><div className="editor-heading"><h2>导图编辑</h2><button disabled={!content || busy} onClick={save}>保存</button></div>
      {content && <p aria-live="polite">{isDirty ? '有未保存的修改' : '所有修改已保存'}</p>}
      {content ? <><label htmlFor="title">导图标题</label><input id="title" value={content.title} maxLength={200} onChange={e => setContent({ ...content, title: e.target.value })} />
        <ul className="tree"><NodeEditor root node={content.root}
          onRename={(id, label) => changeRoot(root => editTree(root, id, node => ({ ...node, label })))}
          onAdd={id => changeRoot(root => editTree(root, id, node => ({ ...node, children: [...node.children, { id: crypto.randomUUID(), label: '新节点', children: [] }] })))}
          onRemove={id => changeRoot(root => removeNode(root, id))} /></ul></> : <p>输入主题后生成，或从左侧打开已保存的导图。</p>}
      <p className="hint">当前为树形列表编辑器；可视化画布及申报书中的其他能力待开发。</p>
    </fieldset></section></div></main>;
}
