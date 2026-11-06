import "@xyflow/react/dist/style.css";
import { Background, Controls, type Edge, Handle, type Node, type NodeProps, Position, ReactFlow } from "@xyflow/react";
import { useMemo } from "react";
import type { Task } from "../api";

// The DAG of a run: steps laid out in layers by dependency depth (no layout library),
// coloured by status. Loaded on demand (React.lazy): React Flow stays out of the main chunk.

export interface StepAnnotation {
  label: string;
  status: string;
}

export interface RunGraphProps {
  tasks: Task[];
  dependencies: { task: string; depends_on: string }[];
  annotations?: Record<string, StepAnnotation[]>;
}

const TONE: Record<string, string> = {
  SUCCEEDED: "success",
  FAILED: "danger",
  RUNNING: "accent",
  PENDING: "neutral",
  SKIPPED: "muted",
  CANCELLED: "neutral",
};

const STATUS_LABEL: Record<string, string> = {
  SUCCEEDED: "готово",
  FAILED: "ошибка",
  RUNNING: "выполняется",
  PENDING: "ожидает",
  SKIPPED: "пропущен",
  CANCELLED: "отменён",
};

type StepData = { task: Task; notes: StepAnnotation[] };

function StepNode({ data }: NodeProps<Node<StepData>>) {
  const { task, notes } = data;
  return (
    <div className={`flow-node tone-${TONE[task.status] ?? "neutral"}`} title={task.error ?? task.key}>
      <Handle type="target" position={Position.Left} />
      <div className="flow-node-head">
        <span className="flow-node-dot" aria-hidden />
        <code className="truncate">{task.key}</code>
      </div>
      <div className="flow-node-meta">
        {STATUS_LABEL[task.status] ?? task.status}
        {task.attempts > 1 ? ` · попыток: ${task.attempts}` : ""}
      </div>
      {notes.map((n, i) => (
        <div key={`${n.label}-${i}`} className={`flow-node-agent status-${n.status}`}>
          {n.label} · {n.status}
        </div>
      ))}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { step: StepNode };

/** Depth of each step: the longest chain of dependencies leading to it. */
export function layers(keys: string[], dependencies: { task: string; depends_on: string }[]): Map<string, number> {
  const parents = new Map<string, string[]>();
  for (const d of dependencies) parents.set(d.task, [...(parents.get(d.task) ?? []), d.depends_on]);
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const visit = (key: string): number => {
    const known = depth.get(key);
    if (known !== undefined) return known;
    if (visiting.has(key)) return 0; // a cycle cannot happen in a DAG; stay safe anyway
    visiting.add(key);
    const value = Math.max(-1, ...(parents.get(key) ?? []).filter((p) => keys.includes(p)).map(visit)) + 1;
    visiting.delete(key);
    depth.set(key, value);
    return value;
  };
  keys.forEach(visit);
  return depth;
}

export default function RunGraph({ tasks, dependencies, annotations = {} }: RunGraphProps) {
  const { nodes, edges } = useMemo(() => {
    const keys = tasks.map((t) => t.key);
    const depth = layers(keys, dependencies);
    const rows = new Map<number, number>();
    const nodes: Node<StepData>[] = tasks.map((task) => {
      const column = depth.get(task.key) ?? 0;
      const row = rows.get(column) ?? 0;
      rows.set(column, row + 1);
      return {
        id: task.key,
        type: "step",
        position: { x: column * 250, y: row * 104 },
        data: { task, notes: annotations[task.key] ?? [] },
        draggable: false,
      };
    });
    const status = new Map(tasks.map((t) => [t.key, t.status]));
    const edges: Edge[] = dependencies
      .filter((d) => status.has(d.task) && status.has(d.depends_on))
      .map((d) => ({
        id: `${d.depends_on}->${d.task}`,
        source: d.depends_on,
        target: d.task,
        animated: status.get(d.task) === "RUNNING",
        className: status.get(d.depends_on) === "FAILED" ? "edge-failed" : undefined,
      }));
    return { nodes, edges };
  }, [tasks, dependencies, annotations]);
  const light = typeof document !== "undefined" && document.documentElement.dataset.theme === "arctic";
  return (
    <div className="run-graph" role="img" aria-label={`Граф шагов: ${tasks.length}, связей: ${edges.length}`}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        colorMode={light ? "light" : "dark"}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
      >
        <Background gap={20} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
