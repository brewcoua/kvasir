import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/svelte";
import type { Step } from "./steps";

export const CARD = { width: 220, height: 64 };
export const STAGE = { width: 240, height: 76 };

export interface StepData extends Record<string, unknown> {
  step: Step;
  open: boolean;
  // Descendants a closed step hides.
  hidden: number;
}

const descendants = (step: Step): number => step.children.reduce((n, child) => n + 1 + descendants(child), 0);

// Roots always, then the children of every step in `open`, left to right.
export function layout(roots: Step[], open: Set<string>): { nodes: Node<StepData>[]; edges: Edge[] } {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "LR", nodesep: 16, ranksep: 48 });
  graph.setDefaultEdgeLabel(() => ({}));

  const steps: Step[] = [];
  const edges: Edge[] = [];
  const link = (source: string, target: string) => {
    edges.push({ id: `${source}>${target}`, source, target });
    graph.setEdge(source, target);
  };
  const visit = (step: Step, size: { width: number; height: number }) => {
    steps.push(step);
    // A copy: dagre writes x and y into the label it is given.
    graph.setNode(step.id, { ...size });
    if (!open.has(step.id)) return;
    for (const child of step.children) {
      visit(child, CARD);
      link(step.id, child.id);
    }
  };
  roots.forEach((root, i) => {
    visit(root, STAGE);
    if (i > 0) link(roots[i - 1].id, root.id);
  });
  dagre.layout(graph);

  const nodes = steps.map((step): Node<StepData> => {
    const { x, y, width, height } = graph.node(step.id);
    const isOpen = open.has(step.id);
    return {
      id: step.id,
      type: "step",
      position: { x: x - width / 2, y: y - height / 2 },
      data: { step, open: isOpen, hidden: isOpen ? 0 : descendants(step) },
    };
  });
  return { nodes, edges };
}
