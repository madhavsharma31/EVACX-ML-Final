"""NetworkX Dijkstra routing with hazard and accessibility constraints."""

from __future__ import annotations

import math
import networkx as nx

STAIR_TYPES = {"stairs", "staircase", "stairway"}
RAMP_TYPES = {"ramp", "wheelchair ramp"}

MOBILITY_TYPES = {
    "normal",
    "wheelchair",
    "elderly",
    "temporary_injury",
    "child",
    "limited_mobility",
}


def edge_cost(graph, source, target, data, mobility="normal") -> float:
    """Return a non-negative traversal cost for one graph edge."""
    if data.get("blocked", False):
        return math.inf

    source_data = graph.nodes[source]
    target_data = graph.nodes[target]
    source_type = source_data.get("type", "")
    target_type = target_data.get("type", "")

    if mobility not in MOBILITY_TYPES:
        mobility = "normal"

    stairs = (
        bool(data.get("stairs", False))
        or source_type in STAIR_TYPES
        or target_type in STAIR_TYPES
        or data.get("edge_type") in STAIR_TYPES
    )

    if mobility == "wheelchair" and (
        stairs or data.get("accessible") is False
        or source_data.get("wheelchair_accessible") is False
        or target_data.get("wheelchair_accessible") is False
    ):
        return math.inf

    cost = max(float(data.get("distance", 1)), 0.1)

    # Hazards are deliberately expensive. A blocked edge is still absolute.
    cost += max(float(data.get("hazard", 0)), 0.0) * 1000
    cost += max(float(data.get("congestion", 0)), 0.0) * 100

    # Node hazard is included too; the previous implementation ignored it.
    cost += max(float(source_data.get("hazard", 0)), 0.0) * 1000
    cost += max(float(target_data.get("hazard", 0)), 0.0) * 1000

    if mobility in {"elderly", "limited_mobility"} and stairs:
        cost += 60 if mobility == "elderly" else 120

    if mobility == "temporary_injury" and stairs:
        cost += 150

    if mobility == "child" and stairs:
        cost += 80

    # Wheelchair users prefer ramps when an accessible alternative exists.
    if mobility == "wheelchair" and (
        data.get("edge_type") in RAMP_TYPES
        or source_type in RAMP_TYPES
        or target_type in RAMP_TYPES
    ):
        cost = max(cost - 10, 0.1)

    return cost


def calculate_route(graph: nx.Graph, mobility="normal") -> dict:
    """Use NetworkX Dijkstra to find the lowest-cost safe exit."""
    start = "start"
    if start not in graph:
        return {"success": False, "message": "Current position unavailable."}

    exits = [
        node for node, data in graph.nodes(data=True)
        if data.get("type") == "exit"
    ]
    if not exits:
        return {"success": False, "message": "No exits available."}

    def weight(source, target, data):
        return edge_cost(graph, source, target, data, mobility)

    best_route = None
    best_exit = None
    best_cost = math.inf

    for exit_node in exits:
        try:
            route = nx.shortest_path(graph, start, exit_node, weight=weight)
            total_cost = sum(
                edge_cost(graph, a, b, graph[a][b], mobility)
                for a, b in zip(route[:-1], route[1:])
            )
            if math.isfinite(total_cost) and total_cost < best_cost:
                best_cost = total_cost
                best_route = route
                best_exit = exit_node
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

    if best_route is None:
        message = (
            "No safe accessible route available."
            if mobility == "wheelchair"
            else "No safe route available."
        )
        return {
            "success": False,
            "message": message,
            "mobility": mobility,
        }

    if best_cost < 100:
        risk = "LOW"
    elif best_cost < 500:
        risk = "MEDIUM"
    else:
        risk = "HIGH"

    # Route confidence combines the weakest node/edge evidence.
    route_conf = 1.0
    for node in best_route:
        route_conf = min(route_conf, float(graph.nodes[node].get("confidence", 1.0)))
    for a, b in zip(best_route[:-1], best_route[1:]):
        route_conf = min(route_conf, float(graph[a][b].get("confidence", 1.0)))

    return {
        "success": True,
        "route": best_route,
        "recommended_exit": graph.nodes[best_exit].get("label", best_exit),
        "exit_id": best_exit,
        "cost": round(best_cost, 2),
        "risk": risk,
        "confidence": round(route_conf, 2),
        "mobility": mobility,
    }
