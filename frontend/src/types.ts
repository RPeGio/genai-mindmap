export interface MindNode { id: string; label: string; children: MindNode[] }
export interface SourceUsage { source_id: string; kind: 'text' | 'markdown' | 'document' | 'image' | 'audio'; filename: string | null; char_count: number }
export interface MindMapContent { title: string; root: MindNode; source_usage?: SourceUsage[] }
export interface MindMapRecord { id: string; version: number; content: MindMapContent; updated_at: string }
export interface Generation { mode: 'demo' | 'model'; content: MindMapContent }
