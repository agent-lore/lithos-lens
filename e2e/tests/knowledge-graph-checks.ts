import { expect, type Page } from "@playwright/test";

/**
 * The knowledge graph canvas's labels read as drawn, at the zoom it is at
 * (K2 S4). Measured on the rendered elements rather than a PNG, which this
 * sandbox cannot read:
 *
 * - every label the payload names is DRAWN: a note's title, and an edge's
 *   raw type or resolution as exactly one of its centre, source or target
 *   label, with real bounds — so a placement that moves a label cannot lose
 *   it;
 * - every label renders at 10px or more (its font size times the zoom);
 * - no two labels share any of the canvas, and no label sits under another
 *   note's circle (model-space boxes: the layout and placement decide them);
 * - the pan hint is shown exactly while part of the graph is outside the
 *   view.
 */
export async function knowledgeCanvasLabelsAreReadable(page: Page) {
  const labels = await page.evaluate(() => {
    const graph = (window as any).LithosLensKnowledgeGraph;
    const cy = graph.cy;
    const zoom = cy.zoom();
    type Box = { id: string; x1: number; x2: number; y1: number; y2: number };
    const textOf = (element: any) =>
      ["label", "source-label", "target-label"]
        .map((key) => element.pstyle(key).strValue)
        .filter((text: string) => text !== "");
    const boxOf = (element: any) =>
      element.boundingBox({
        includeNodes: false,
        includeEdges: false,
        includeLabels: true,
        includeOverlays: false,
      });
    const byPid = (collection: any, pid: string) =>
      collection.filter((element: any) => element.data("pid") === pid)[0];
    const undrawn: string[] = [];
    const expected: Array<[string, string, any]> = [
      ...graph.payload.nodes.map((node: any) => [node.id, node.label, byPid(cy.nodes(), node.id)]),
      ...graph.payload.edges
        .filter((edge: any) => edge.style && edge.style.label)
        .map((edge: any) => [edge.id, edge.style.label, byPid(cy.edges(), edge.id)]),
    ];
    expected.forEach(([id, want, element]) => {
      const texts = element ? textOf(element) : [];
      const box = element ? boxOf(element) : { w: 0, h: 0 };
      const real = Number.isFinite(box.w) && Number.isFinite(box.h) && box.w > 0 && box.h > 0;
      if (texts.length !== 1 || texts[0] !== want || !real) {
        undrawn.push(`${id}: ${JSON.stringify(texts)} for ${JSON.stringify(want)}`);
      }
    });
    const boxes: Box[] = [];
    const small: Array<[string, number]> = [];
    cy.elements().forEach((element: any) => {
      if (!element.data("label")) return;
      const rendered = element.pstyle("font-size").pfValue * zoom;
      if (rendered < 10) small.push([element.data("pid"), rendered]);
      const box = boxOf(element);
      boxes.push({ id: element.data("pid"), x1: box.x1, x2: box.x2, y1: box.y1, y2: box.y2 });
    });
    const apart = (a: Box, b: Box) =>
      a.x2 <= b.x1 || b.x2 <= a.x1 || a.y2 <= b.y1 || b.y2 <= a.y1;
    const overlaps: string[] = [];
    boxes.forEach((a, i) => {
      boxes.slice(i + 1).forEach((b) => {
        if (!apart(a, b)) overlaps.push(`${a.id} × ${b.id}`);
      });
    });
    // …nor under a note's circle: any label but a note's own title.
    cy.nodes().forEach((node: any) => {
      const body = node.boundingBox({ includeLabels: false, includeOverlays: false });
      const circle = { id: node.data("pid"), x1: body.x1, x2: body.x2, y1: body.y1, y2: body.y2 };
      boxes.forEach((label) => {
        if (label.id !== circle.id && !apart(label, circle)) {
          overlaps.push(`${label.id} × body of ${circle.id}`);
        }
      });
    });
    const drawn = cy.elements().renderedBoundingBox();
    const clipped =
      drawn.x1 < -1 || drawn.y1 < -1 || drawn.x2 > cy.width() + 1 || drawn.y2 > cy.height() + 1;
    const hint = document.querySelector("[data-kgraph-pan-hint]") as HTMLElement;
    return { undrawn, small, overlaps, clipped, hinted: !!hint && !hint.hidden };
  });
  expect(labels.undrawn).toEqual([]);
  expect(labels.small).toEqual([]);
  expect(labels.overlaps).toEqual([]);
  expect(labels.hinted).toBe(labels.clipped);
}
