"""Build a conservative NetworkX navigation graph from visual detections.

The graph is intentionally spatial/semantic rather than a literal floor plan:
detections become landmarks and nearby landmarks are connected.  The routing
engine remains NetworkX Dijkstra; this module only builds the graph.
"""

from __future__ import annotations

import math
import networkx as nx

EXIT_CONFIDENCE_THRESHOLD = 0.10
MAX_CONNECTION_DISTANCE = 1000
NEAREST_NEIGHBORS = 3

STAIR_TYPES = {"stairs", "staircase", "stairway"}
RAMP_TYPES = {"ramp", "wheelchair ramp"}
VERTICAL_TYPES = STAIR_TYPES | RAMP_TYPES | {"elevator", "lift"}
NAV_TYPES = {
    "door", "doorway", "room door", "office door", "wooden door",
    "glass door", "entrance", "corridor", "hallway",
    *VERTICAL_TYPES,
}


def center(bbox: list[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def distance(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _is_stairs(data: dict) -> bool:
    return data.get("type") in STAIR_TYPES


def _is_ramp(data: dict) -> bool:
    return data.get("type") in RAMP_TYPES


def _add_edge(graph: nx.Graph, a: str, b: str, d: float) -> None:
    """Add a semantic edge with consistent routing attributes."""
    da, db = graph.nodes[a], graph.nodes[b]
    stairs = _is_stairs(da) or _is_stairs(db)
    accessible = not stairs
    edge_type = (
        "stairs" if stairs
        else "ramp" if (_is_ramp(da) or _is_ramp(db))
        else "exit" if da.get("type") == "exit" or db.get("type") == "exit"
        else "corridor"
    )
    graph.add_edge(
        a,
        b,
        distance=max(float(d), 0.1),
        hazard=0.0,
        congestion=0.0,
        blocked=False,
        stairs=stairs,
        accessible=accessible,
        edge_type=edge_type,
    )


def _dedupe_exits(detections: list[dict], radius: float = 80) -> list[dict]:
    unique: list[dict] = []
    for candidate in sorted(
        detections, key=lambda d: float(d.get("confidence", 0)), reverse=True
    ):
        cx, cy = center(candidate["bbox"])
        if all(
            math.hypot(cx - center(existing["bbox"])[0],
                       cy - center(existing["bbox"])[1]) >= radius
            for existing in unique
        ):
            unique.append(candidate)
    return unique


def build_graph(detections: list[dict]) -> tuple[nx.Graph, list[str]]:
    """Create a conservative spatial navigation graph.

    We connect only nearby semantic landmarks and guarantee a fallback
    connection from ``start`` to exits when no architectural landmarks exist.
    This avoids the old all-to-all graph that made every exit appear directly
    reachable through walls.
    """
    graph = nx.Graph()

    people = [d for d in detections if d.get("type") == "person"]
    exits = _dedupe_exits([
        d for d in detections
        if d.get("type") in {"exit sign", "exit door", "emergency exit"}
        and float(d.get("confidence", 0)) >= EXIT_CONFIDENCE_THRESHOLD
    ])

    if people:
        user = max(people, key=lambda d: float(d.get("confidence", 0)))
        user_x, user_y = center(user["bbox"])
        user_conf = float(user.get("confidence", 0))
    else:
        user_x, user_y, user_conf = 0.5, 0.5, 0.25

    graph.add_node(
        "start",
        type="current_position",
        label="YOU ARE HERE",
        x=user_x,
        y=user_y,
        confidence=user_conf,
        hazard=0.0,
        congestion=0.0,
        wheelchair_accessible=True,
    )

    exit_nodes: list[str] = []
    for i, det in enumerate(exits, 1):
        x, y = center(det["bbox"])
        node_id = f"exit_{i}"
        graph.add_node(
            node_id,
            type="exit",
            label=f"EXIT {i}",
            x=x,
            y=y,
            confidence=float(det.get("confidence", 0)),
            hazard=0.0,
            congestion=0.0,
            wheelchair_accessible=True,
        )
        exit_nodes.append(node_id)

    architectural_nodes: list[str] = []
    for index, det in enumerate(detections):
        dtype = det.get("type", "")
        if dtype not in NAV_TYPES:
            continue
        x, y = center(det["bbox"])
        node_id = f"environment_{index}"
        graph.add_node(
            node_id,
            type=dtype,
            label=dtype.replace("_", " ").upper(),
            x=x,
            y=y,
            confidence=float(det.get("confidence", 0)),
            hazard=0.0,
            congestion=0.0,
            wheelchair_accessible=dtype not in STAIR_TYPES,
        )
        architectural_nodes.append(node_id)

    navigational = architectural_nodes + exit_nodes

    # Connect each landmark to a few nearest landmarks, not every landmark.
    for node_id in navigational:
        candidates = [
            other for other in navigational
            if other != node_id
            and not (
                graph.nodes[node_id].get("type") == "exit"
                and graph.nodes[other].get("type") == "exit"
            )
        ]
        candidates.sort(key=lambda other: distance(graph.nodes[node_id], graph.nodes[other]))
        for other in candidates[:NEAREST_NEIGHBORS]:
            d = distance(graph.nodes[node_id], graph.nodes[other])
            if d <= MAX_CONNECTION_DISTANCE:
                _add_edge(graph, node_id, other, d)

    # Start connects to the nearest corridor/vertical/door landmarks.
    start_candidates = [
        n for n in architectural_nodes
        if graph.nodes[n].get("type") != "exit"
    ]
    start_candidates.sort(key=lambda n: distance(graph.nodes["start"], graph.nodes[n]))

    if start_candidates:
        for n in start_candidates[:NEAREST_NEIGHBORS]:
            d = distance(graph.nodes["start"], graph.nodes[n])
            if d <= MAX_CONNECTION_DISTANCE:
                _add_edge(graph, "start", n, d)

        # If an exit is very close to the observed user, connect it too.
        nearest_exit = min(exit_nodes, key=lambda n: distance(graph.nodes["start"], graph.nodes[n]), default=None)
        if nearest_exit is not None:
            d = distance(graph.nodes["start"], graph.nodes[nearest_exit])
            if d <= MAX_CONNECTION_DISTANCE * 0.5:
                _add_edge(graph, "start", nearest_exit, d)
    else:
        # No architecture detected: direct exit connection is the only
        # defensible fallback, and the returned confidence remains low.
        for n in exit_nodes:
            d = distance(graph.nodes["start"], graph.nodes[n])
            if d <= MAX_CONNECTION_DISTANCE:
                _add_edge(graph, "start", n, d)

    # Guarantee every exit is reachable when an architectural graph exists:
    # attach isolated exits to their nearest non-exit landmark.
    for ex in exit_nodes:
        if graph.degree(ex) == 0 and start_candidates:
            nearest = min(start_candidates, key=lambda n: distance(graph.nodes[ex], graph.nodes[n]))
            d = distance(graph.nodes[ex], graph.nodes[nearest])
            if d <= MAX_CONNECTION_DISTANCE:
                _add_edge(graph, ex, nearest, d)

    return graph, exit_nodes
