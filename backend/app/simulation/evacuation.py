"""Evacuation simulation engine.

Takes a corrected floor plan → builds navigation graph → applies
hazard constraints → applies mobility constraints → routes each occupant.

Core workflow:
    FLOOR PLAN ELEMENTS
            ↓
    NAVIGATION GRAPH (nodes + edges)
            ↓
    FIRE / HAZARD APPLICATION
            ↓
    PER-OCCUPANT MOBILITY CONSTRAINTS
            ↓
    ROUTE CALCULATION
            ↓
    VALIDATED EVACUATION RESULTS
"""

from __future__ import annotations

import math
import logging
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)


# ================================================================
# Constants
# ================================================================

MOBILITY_TYPES = {"normal", "wheelchair", "limited_mobility", "elderly", "temporary_injury", "child"}

# Edges that are forbidden for wheelchair users
WHEELCHAIR_FORBIDDEN_TYPES = {"stairs", "staircase", "stairway"}

# Default hazard costs
DEFAULT_FIRE_BLOCK_COST = 10000
DEFAULT_ADJACENT_PENALTY = 500


# ================================================================
# Graph construction from floor plan elements
# ================================================================

def build_navigation_graph(elements: list[dict]) -> nx.Graph:
    """Build a semantic, corridor-backbone navigation graph.

    The old implementation connected nearby rectangles directly.  That can
    create unrealistic diagonal routes through rooms/walls.  This version
    explicitly models the circulation backbone:

        room -> its door -> corridor waypoint -> corridor backbone ->
        exit/stairs/ramp/elevator

    Waypoints are placed where each access point meets the corridor, so the
    returned NetworkX path is also suitable for drawing a believable route.
    """
    graph = nx.Graph()

    # Normalize the supplied elements into lookup data first.
    by_id: dict[str, dict] = {}
    for idx, el in enumerate(elements):
        node_id = str(el.get("id", f"node_{idx}"))
        by_id[node_id] = el
        el_type = str(el.get("type", "unknown"))
        x = float(el.get("x", 0)) + float(el.get("width", 0)) / 2
        y = float(el.get("y", 0)) + float(el.get("height", 0)) / 2
        confidence = float(el.get("confidence", 1.0))
        graph.add_node(
            node_id,
            type=el_type,
            x=x,
            y=y,
            confidence=confidence,
            source=el.get("source", "ai_detected"),
            hazard=0.0,
            congestion=0.0,
            wheelchair_accessible=el_type not in WHEELCHAIR_FORBIDDEN_TYPES,
        )

    corridors = [
        (node_id, el) for node_id, el in by_id.items()
        if str(el.get("type", "")) in {"corridor", "hallway"}
    ]

    def center(el: dict) -> tuple[float, float]:
        return (
            float(el.get("x", 0)) + float(el.get("width", 0)) / 2,
            float(el.get("y", 0)) + float(el.get("height", 0)) / 2,
        )

    def rect_gap(a: dict, b: dict) -> float:
        ax, ay = float(a.get("x", 0)), float(a.get("y", 0))
        aw, ah = float(a.get("width", 0)), float(a.get("height", 0))
        bx, by = float(b.get("x", 0)), float(b.get("y", 0))
        bw, bh = float(b.get("width", 0)), float(b.get("height", 0))
        dx = max(0.0, max(ax, bx) - min(ax + aw, bx + bw))
        dy = max(0.0, max(ay, by) - min(ay + ah, by + bh))
        return math.hypot(dx, dy)

    def add_edge(a: str, b: str, edge_type: str, distance: float,
                 accessible: bool = True, confidence: float = 0.9) -> None:
        if a == b:
            return
        graph.add_edge(
            a, b,
            distance=max(float(distance), 0.1),
            edge_type=edge_type,
            accessible=accessible,
            hazard=0.0,
            congestion=0.0,
            blocked=False,
            confidence=confidence,
            stairs=edge_type in WHEELCHAIR_FORBIDDEN_TYPES,
        )

    # ------------------------------------------------------------------
    # If there is a corridor, use it as the circulation backbone.
    # ------------------------------------------------------------------
    if corridors:
        # The current generator normally creates one corridor.  We support
        # multiple corridors by building a backbone for each one.
        waypoint_ids: list[str] = []
        access_types = {
            "door", "room door", "office door", "wooden door", "glass door",
            "exit", "stairs", "staircase", "stairway", "ramp", "wheelchair ramp",
            "elevator", "lift",
        }

        for corr_index, (corr_id, corr_el) in enumerate(corridors):
            cx, cy = center(corr_el)
            c_x, c_y = float(corr_el.get("x", 0)), float(corr_el.get("y", 0))
            c_w, c_h = float(corr_el.get("width", 0)), float(corr_el.get("height", 0))
            horizontal = c_w >= c_h

            # Access elements close enough to this corridor are attached to a
            # point on the corridor boundary/centreline, not directly to one
            # another.
            access_nodes = []
            for node_id, el in by_id.items():
                et = str(el.get("type", ""))
                if node_id == corr_id or et not in access_types:
                    continue
                if rect_gap(el, corr_el) <= 70:
                    access_nodes.append((node_id, el))

            # Project each access point onto the corridor centreline.  Clamp
            # to corridor bounds so routes cannot jump outside the corridor.
            projections: list[tuple[str, float, float]] = []
            for node_id, el in access_nodes:
                ex, ey = center(el)
                if horizontal:
                    px = max(c_x + 8, min(c_x + c_w - 8, ex))
                    py = cy
                else:
                    px = cx
                    py = max(c_y + 8, min(c_y + c_h - 8, ey))
                waypoint_id = f"corridor_wp_{corr_index}_{len(projections)}"
                graph.add_node(
                    waypoint_id,
                    type="corridor_waypoint",
                    label="CORRIDOR",
                    x=px,
                    y=py,
                    confidence=min(
                        float(el.get("confidence", 0.5)),
                        float(corr_el.get("confidence", 0.5)),
                    ),
                    hazard=0.0,
                    congestion=0.0,
                    wheelchair_accessible=True,
                    corridor_id=corr_id,
                )
                waypoint_ids.append(waypoint_id)
                projections.append((waypoint_id, px, py))

                ex, ey = center(el)
                et = str(el.get("type", ""))
                if et == "door" or "door" in et:
                    # Rooms are connected through their declared door only.
                    add_edge(
                        node_id, waypoint_id, "door",
                        math.hypot(ex - px, ey - py),
                        accessible=bool(el.get("wheelchair_accessible", True)),
                        confidence=float(el.get("confidence", 0.5)),
                    )
                elif et in WHEELCHAIR_FORBIDDEN_TYPES:
                    add_edge(node_id, waypoint_id, "stairs", math.hypot(ex-px, ey-py), accessible=False,
                             confidence=float(el.get("confidence", 0.5)))
                elif et in {"ramp", "wheelchair ramp"}:
                    add_edge(node_id, waypoint_id, "ramp", math.hypot(ex-px, ey-py), accessible=True,
                             confidence=float(el.get("confidence", 0.5)))
                elif et in {"elevator", "lift"}:
                    add_edge(node_id, waypoint_id, "elevator", math.hypot(ex-px, ey-py), accessible=True,
                             confidence=float(el.get("confidence", 0.5)))
                else:
                    # Exit from its boundary to the corridor waypoint.
                    add_edge(node_id, waypoint_id, "exit", math.hypot(ex-px, ey-py), accessible=True,
                             confidence=float(el.get("confidence", 0.5)))

                # Attach the room that explicitly declares this door.
                for room_id, room in by_id.items():
                    if str(room.get("type", "")) != "room":
                        continue
                    if str(room.get("connected_door_id", "")) == node_id:
                        rx, ry = center(room)
                        add_edge(
                            room_id, node_id, "door",
                            math.hypot(rx - ex, ry - ey),
                            accessible=bool(room.get("wheelchair_accessible", True)) and
                                      bool(el.get("wheelchair_accessible", True)),
                            confidence=min(float(room.get("confidence", 0.5)), float(el.get("confidence", 0.5))),
                        )

            # Connect corridor waypoints in physical order. This is the key
            # improvement: movement along a corridor follows its backbone.
            projections.sort(key=lambda p: p[1] if horizontal else p[2])
            for a, b in zip(projections[:-1], projections[1:]):
                d = math.hypot(a[1] - b[1], a[2] - b[2])
                add_edge(a[0], b[0], "corridor", d, accessible=True, confidence=0.85)

            # A single access point still needs a backbone node. Use a center
            # waypoint so start/occupants can enter the corridor naturally.
            if not projections:
                waypoint_id = f"corridor_center_{corr_index}"
                graph.add_node(
                    waypoint_id, type="corridor_waypoint", label="CORRIDOR",
                    x=cx, y=cy, confidence=float(corr_el.get("confidence", 0.5)),
                    hazard=0.0, congestion=0.0, wheelchair_accessible=True,
                    corridor_id=corr_id,
                )
                waypoint_ids.append(waypoint_id)

        # Connect separate corridor backbones only when their rectangles are
        # actually close, avoiding arbitrary long-distance jumps.
        corridor_centers = [(cid, center(el)) for cid, el in corridors]
        for i, (cid_a, (ax, ay)) in enumerate(corridor_centers):
            for cid_b, (bx, by) in corridor_centers[i + 1:]:
                if math.hypot(ax - bx, ay - by) <= 120:
                    a_wp = next((n for n in waypoint_ids if graph.nodes[n].get("corridor_id") == cid_a), None)
                    b_wp = next((n for n in waypoint_ids if graph.nodes[n].get("corridor_id") == cid_b), None)
                    if a_wp and b_wp:
                        add_edge(a_wp, b_wp, "corridor", math.hypot(ax-bx, ay-by), True, 0.7)

    else:
        # No corridor was reconstructed. Fall back to conservative door/room
        # connectivity rather than an all-to-all Euclidean graph.
        doors = [n for n, d in graph.nodes(data=True) if "door" in d.get("type", "")]
        for room_id, room in by_id.items():
            if str(room.get("type", "")) != "room":
                continue
            door_id = str(room.get("connected_door_id", ""))
            if door_id in graph.nodes:
                rx, ry = center(room)
                dx, dy = center(by_id[door_id])
                add_edge(room_id, door_id, "door", math.hypot(rx-dx, ry-dy), True, 0.6)
        for a_i, a_id in enumerate(doors):
            for b_id in doors[a_i + 1:]:
                if rect_gap(by_id[a_id], by_id[b_id]) <= 70:
                    ax, ay = center(by_id[a_id]); bx, by = center(by_id[b_id])
                    add_edge(a_id, b_id, "door", math.hypot(ax-bx, ay-by), True, 0.5)

    # ------------------------------------------------------------------
    # Add a single start node. In simulation mode the actual occupant is
    # mapped to its selected room, so this is only used for compatibility.
    # ------------------------------------------------------------------
    if "start" not in graph:
        graph.add_node("start", type="current_position", label="YOU ARE HERE",
                       x=0, y=0, confidence=0.25, hazard=0.0, congestion=0.0,
                       wheelchair_accessible=True)

    # Exits must connect through corridor waypoints (or a nearby door in the
    # no-corridor fallback). Never create direct start -> exit edges here.
    return graph


def _infer_edge_type(type_a: str, type_b: str) -> str:
    """Infer the edge type from the two connected node types."""
    for t in (type_a, type_b):
        if t in WHEELCHAIR_FORBIDDEN_TYPES:
            return "stairs"
        if t == "ramp":
            return "ramp"
        if t == "elevator":
            return "elevator"
        if t == "door":
            return "door"
    return "corridor"


# ================================================================
# Hazard application
# ================================================================

def apply_hazard(
    graph: nx.Graph,
    fire_room_id: str,
    block_cost: float = DEFAULT_FIRE_BLOCK_COST,
    adjacent_penalty: float = DEFAULT_ADJACENT_PENALTY,
) -> dict:
    """Mark fire room as hazardous and penalise nearby nodes.

    Returns a dict describing what was blocked.
    """
    blocked_nodes = []
    hazard_edges = 0

    if fire_room_id not in graph.nodes:
        logger.warning("[SIM] Fire room %s not in graph", fire_room_id)
        return {"blocked_nodes": blocked_nodes, "hazard_edges": hazard_edges}

    # Block the fire room itself
    graph.nodes[fire_room_id]["hazard"] = block_cost
    blocked_nodes.append(fire_room_id)

    # Penalise adjacent nodes (one hop away)
    for neighbor in graph.neighbors(fire_room_id):
        current = graph.nodes[neighbor].get("hazard", 0)
        graph.nodes[neighbor]["hazard"] = current + adjacent_penalty

    # Block / heavily penalise edges touching the fire room
    for neighbor in list(graph.neighbors(fire_room_id)):
        edge_data = graph[fire_room_id][neighbor]
        edge_data["hazard"] = block_cost
        edge_data["blocked"] = True
        hazard_edges += 1

    # Penalise edges to adjacent nodes (not blocked, just expensive)
    for node_a in blocked_nodes:
        for neighbor in graph.neighbors(node_a):
            if neighbor not in blocked_nodes:
                edge_data = graph[node_a][neighbor]
                if not edge_data.get("blocked", False):
                    edge_data["hazard"] = max(
                        edge_data.get("hazard", 0),
                        adjacent_penalty,
                    )

    logger.info(
        "[SIM] Hazard applied: %s blocked, %d adjacent penalised, %d edges blocked",
        fire_room_id,
        len(blocked_nodes) - 1,
        hazard_edges,
    )

    return {"blocked_nodes": blocked_nodes, "hazard_edges": hazard_edges}


# ================================================================
# Mobility constraints
# ================================================================

def apply_mobility_constraints(
    graph: nx.Graph,
    mobility: str,
) -> nx.Graph:
    """Return a derived graph with mobility-incompatible edges removed.

    For wheelchair users, stairs edges are removed entirely.
    The original graph is NOT mutated.
    """
    g = graph.copy()

    if mobility == "wheelchair":
        edges_to_remove = []
        for u, v, data in g.edges(data=True):
            edge_type = data.get("edge_type", "")
            target_type_u = g.nodes[u].get("type", "")
            target_type_v = g.nodes[v].get("type", "")

            if (
                edge_type in WHEELCHAIR_FORBIDDEN_TYPES
                or target_type_u in WHEELCHAIR_FORBIDDEN_TYPES
                or target_type_v in WHEELCHAIR_FORBIDDEN_TYPES
            ):
                edges_to_remove.append((u, v))

        g.remove_edges_from(edges_to_remove)
        logger.info(
            "[SIM] Wheelchair constraints: removed %d stairs edges",
            len(edges_to_remove),
        )

    elif mobility == "limited_mobility":
        # Higher cost for stairs, but not forbidden
        for u, v, data in g.edges(data=True):
            if data.get("edge_type") in WHEELCHAIR_FORBIDDEN_TYPES:
                data["hazard"] = data.get("hazard", 0) + 2000

    elif mobility == "elderly":
        for u, v, data in g.edges(data=True):
            if data.get("edge_type") in WHEELCHAIR_FORBIDDEN_TYPES:
                data["hazard"] = data.get("hazard", 0) + 500

    elif mobility == "temporary_injury":
        for u, v, data in g.edges(data=True):
            if data.get("edge_type") in WHEELCHAIR_FORBIDDEN_TYPES:
                data["hazard"] = data.get("hazard", 0) + 1000

    elif mobility == "child":
        for u, v, data in g.edges(data=True):
            if data.get("edge_type") in WHEELCHAIR_FORBIDDEN_TYPES:
                data["hazard"] = data.get("hazard", 0) + 600

    return g


# ================================================================
# Route calculation
# ================================================================

def calculate_route_for_occupant(
    graph: nx.Graph,
    start_node: str,
    mobility: str = "normal",
) -> dict:
    """Find the best evacuation route from start_node to the nearest exit.

    Returns a result dict with route details.
    """
    if start_node not in graph.nodes:
        return {
            "success": False,
            "reason": "START_NOT_FOUND",
            "message": f"Occupant location '{start_node}' not found in building.",
        }

    # Find exits
    exits = [
        n for n, d in graph.nodes(data=True)
        if d.get("type") == "exit"
    ]

    if not exits:
        return {
            "success": False,
            "reason": "NO_EXITS",
            "message": "No exits found in the building.",
        }

    # Apply mobility constraints
    constrained_graph = apply_mobility_constraints(graph, mobility)

    # Check start still exists (might have been disconnected)
    if start_node not in constrained_graph.nodes:
        return {
            "success": False,
            "reason": "DISCONNECTED",
            "message": "Occupant location is disconnected in this mobility mode.",
        }

    # Find reachable exits
    reachable_exits = []
    for ex in exits:
        if ex in constrained_graph.nodes:
            reachable_exits.append(ex)

    if not reachable_exits:
        if mobility == "wheelchair":
            return {
                "success": False,
                "reason": "NO_ACCESSIBLE_ROUTE",
                "message": "No safe accessible route to an exit is currently available.",
            }
        return {
            "success": False,
            "reason": "NO_ROUTE",
            "message": "No safe route to an exit is currently available.",
        }

    # Route to each exit, pick best
    def weight(u, v, data):
        if data.get("blocked", False):
            return float("inf")
        cost = data.get("distance", 1)
        cost += data.get("hazard", 0)
        cost += data.get("congestion", 0) * 100
        return cost

    best_route = None
    best_exit = None
    best_cost = float("inf")

    for ex in reachable_exits:
        try:
            route = nx.shortest_path(
                constrained_graph, start_node, ex, weight=weight
            )
            total_cost = sum(
                weight(a, b, constrained_graph[a][b])
                for a, b in zip(route[:-1], route[1:])
            )
            if total_cost < best_cost:
                best_cost = total_cost
                best_route = route
                best_exit = ex
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

    if best_route is None:
        return {
            "success": False,
            "reason": "NO_ROUTE",
            "message": "No safe route to an exit is currently available.",
        }

    # Analyse route for accessibility info
    uses_stairs = False
    uses_ramp = False
    uses_elevator = False
    for a, b in zip(best_route[:-1], best_route[1:]):
        edge_data = constrained_graph[a][b]
        et = edge_data.get("edge_type", "")
        if et == "stairs":
            uses_stairs = True
        elif et == "ramp":
            uses_ramp = True
        elif et == "elevator":
            uses_elevator = True

    # Risk assessment
    if best_cost < 100:
        risk = "LOW"
    elif best_cost < 500:
        risk = "MEDIUM"
    else:
        risk = "HIGH"

    return {
        "success": True,
        "route": best_route,
        "recommended_exit": constrained_graph.nodes[best_exit].get("label", best_exit),
        "exit_id": best_exit,
        "cost": round(best_cost, 2),
        "risk": risk,
        "mobility": mobility,
        "accessible_route": mobility == "wheelchair",
        "uses_stairs": uses_stairs,
        "uses_ramp": uses_ramp,
        "uses_elevator": uses_elevator,
        "distance": round(best_cost, 2),
    }


# ================================================================
# Full evacuation simulation
# ================================================================

def run_evacuation(
    floor_plan_elements: list[dict],
    fire_room_id: str,
    occupants: list[dict],
) -> dict:
    """Run a complete evacuation simulation.

    Parameters
    ----------
    floor_plan_elements : list of floor plan element dicts
    fire_room_id : ID of the room where fire starts
    occupants : list of dicts with keys: id, name, location_id, mobility

    Returns
    -------
    dict with hazard info and per-occupant evacuation results
    """
    logger.info(
        "[SIM] Evacuation: fire=%s, occupants=%d",
        fire_room_id,
        len(occupants),
    )

    # 1. Build navigation graph
    graph = build_navigation_graph(floor_plan_elements)
    logger.info("[SIM] Graph: %d nodes, %d edges", graph.number_of_nodes(), graph.number_of_edges())

    # 2. Apply hazard
    hazard_info = apply_hazard(graph, fire_room_id)

    # 3. Route each occupant
    #
    # Important fire semantics:
    # The fire room is blocked for people who are elsewhere, but an occupant
    # who is already inside the fire room MUST be allowed to leave it.  The
    # previous implementation blocked every edge touching the fire room,
    # which made the correct emergency case return "NO_ROUTE".
    evacuations = []
    for occ in occupants:
        occ_id = occ.get("id", "unknown")
        location = occ.get("location_id", "")
        mobility = occ.get("mobility", "normal")
        name = occ.get("name", occ_id)

        # Give each occupant an isolated view of the hazard graph.  If this
        # occupant starts in the fire room, reopen only the room's immediate
        # connections so they can escape; other occupants still cannot enter
        # that room.
        occupant_graph = graph.copy()
        if location == fire_room_id and location in occupant_graph:
            for neighbor in occupant_graph.neighbors(location):
                edge_data = occupant_graph[location][neighbor]
                edge_data["blocked"] = False
                # Keep a strong penalty so the route leaves the hazardous
                # area immediately rather than treating it as normal space.
                edge_data["hazard"] = max(
                    float(edge_data.get("hazard", 0)),
                    DEFAULT_ADJACENT_PENALTY,
                )

        result = calculate_route_for_occupant(occupant_graph, location, mobility)
        result["occupant_id"] = occ_id
        result["occupant_name"] = name
        result["location_id"] = location
        evacuations.append(result)

    logger.info(
        "[SIM] Results: %d/%d successful",
        sum(1 for e in evacuations if e["success"]),
        len(evacuations),
    )

    return {
        "success": True,
        "hazard": {
            "type": "fire",
            "room_id": fire_room_id,
            **hazard_info,
        },
        "graph": {
            "nodes": [
                {"id": n, **d}
                for n, d in graph.nodes(data=True)
            ],
            "edges": [
                {"source": u, "target": v, **d}
                for u, v, d in graph.edges(data=True)
            ],
        },
        "evacuations": evacuations,
    }
