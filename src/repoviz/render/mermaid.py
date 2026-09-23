"""Serialize view graphs to Mermaid flowchart text.

Visual conventions (never colour alone):

========== ====================================================================
added      green fill, solid thick border, "✚" icon, "added" text
removed    red fill, *dashed* border, "✖" icon, "removed" text
modified   amber fill, solid thick border, "✎" icon, "modified" text
unchanged  neutral fill, thin border
edge +     green thick arrow (``==>``) labelled "+ new"
edge −     red dashed arrow labelled "− removed"
edge ~     amber arrow labelled "~ changed"
edge =     thin gray arrow
cycle      purple dashed arrow labelled "⟲ cycle" (combined with the status marker)
========== ====================================================================
"""

from __future__ import annotations

from typing import Any

from .theme import theme
from .views import VEdge, VNode, ViewGraph

__all__ = ["escape", "theme", "to_mermaid"]


_ESCAPES = {'"': "#quot;", "<": "#lt;", ">": "#gt;", "#": "#35;", "&": "#amp;", "`": "#96;", "\n": " ",
            "\r": " ", "\t": " ", "|": "#124;", "[": "#91;", "]": "#93;", "{": "#123;", "}": "#125;"}


def escape(text: str, limit: int = 120) -> str:
    """Escape arbitrary text for use inside a double-quoted Mermaid label."""
    text = str(text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


_SHAPES = {
    "box": ('["', '"]'), "round": ('("', '")'), "stadium": ('(["', '"])'), "hexagon": ('{{"', '"}}'),
    "cylinder": ('[("', '")]'), "subroutine": ('[["', '"]]'),
}


def _class_defs(prefix: str, table: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for name, st in table.items():
        parts = [f"fill:{st['fill']}", f"stroke:{st['stroke']}", f"color:{st['color']}",
                 f"stroke-width:{st['width']}px"]
        if st.get("dash"):
            parts.append(f"stroke-dasharray:{st['dash'].replace(' ', ' ')}")
        out.append(f"  classDef {prefix}{name} {','.join(parts)}")
    return out


def _node_label(n: VNode, mode: str) -> str:
    st = theme()["status"].get(n.status, {})
    marker = f"{st.get('icon', '')} " if mode == "diff" and n.status != "unchanged" else ""
    icon = f"{n.icon} " if n.icon else ""
    first = f"{marker}{icon}{escape(n.label)}"
    second = n.sublabel
    if mode == "diff" and n.status != "unchanged":
        second = f"{st.get('word', n.status)}" + (f" · {n.sublabel}" if n.sublabel else "")
    elif mode == "role" and n.status != "unchanged":
        second = f"{second} · {st.get('word', n.status)}"
    return first + (f"<br/><small>{escape(second, 80)}</small>" if second else "")


def _edge_style(e: VEdge) -> tuple[str, str, str]:
    """Return (arrow, label, linkStyle) for an edge."""
    t = theme()["edge"]
    st = t.get(e.status, t["unchanged"])
    arrow = st["arrow"]
    markers = [st["marker"]] if st["marker"] else []
    style = st
    if e.cycle:
        style = t["cycle"]
        arrow = "-.->" if e.status != "added" else "==>"
        markers.append("⟲ new cycle" if e.cycle_introduced else t["cycle"]["marker"])
    if e.count > 1:
        markers.append(f"×{e.count}")
    if e.relationship not in ("imports", "contains", "calls"):
        markers.insert(0, e.relationship)
    parts = [f"stroke:{style['stroke']}", f"stroke-width:{style['width']}px", "fill:none"]
    if style.get("dash"):
        parts.append(f"stroke-dasharray:{style['dash']}")
    if e.relationship == "contains":
        arrow = "---"
    return arrow, " ".join(markers), ",".join(parts)


def to_mermaid(view: ViewGraph, *, acc_title: str | None = None) -> str:
    th = theme()
    lines = [f"flowchart {view.direction}"]
    lines.append(f"  accTitle: {escape(acc_title or view.title, 200).replace(':', ' -')}")
    counts: dict[str, int] = {}
    for n in view.nodes:
        counts[n.status] = counts.get(n.status, 0) + 1
    desc = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    lines.append(f"  accDescr: {len(view.nodes)} nodes and {len(view.edges)} edges ({escape(desc, 200)})")
    by_parent: dict[str | None, list[VNode]] = {}
    for n in view.nodes:
        parent = n.parent if n.parent in view.subgraphs else None
        by_parent.setdefault(parent, []).append(n)

    def node_line(n: VNode, indent: str) -> str:
        open_, close = _SHAPES.get(n.shape, _SHAPES["box"])
        cls = f"st_{n.status}" if view.mode == "diff" else (f"role_{n.kind}" if view.mode == "role" else f"kind_{n.kind}")
        if view.mode == "role" and n.status in ("added", "removed"):
            cls = f"st_{n.status}"
        return f"{indent}{n.id}{open_}{_node_label(n, view.mode)}{close}:::{cls}"

    for sg_id, (label, _parent) in view.subgraphs.items():
        members = by_parent.get(sg_id, [])
        if not members:
            continue
        lines.append(f'  subgraph {sg_id}["{escape(label)}"]')
        lines.append("    direction TB" if view.direction in ("LR", "RL") else "    direction LR")
        for n in members:
            lines.append(node_line(n, "    "))
        lines.append("  end")
    for n in by_parent.get(None, []):
        lines.append(node_line(n, "  "))
    link_styles = []
    for i, e in enumerate(view.edges):
        arrow, label, style = _edge_style(e)
        if label and arrow != "---":
            lines.append(f'  {e.source} {arrow}|"{escape(label, 60)}"| {e.target}')
        else:
            lines.append(f"  {e.source} {arrow} {e.target}")
        link_styles.append(f"  linkStyle {i} {style}")
    lines += link_styles
    lines += _class_defs("st_", th["status"])
    lines += _class_defs("kind_", th["kind"])
    lines += _class_defs("role_", th["role"])
    return "\n".join(lines) + "\n"
