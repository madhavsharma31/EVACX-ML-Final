/**
 * Shared evacuation result types.
 *
 * Route computation lives in the FastAPI/NetworkX backend. Keeping only
 * contracts here prevents the frontend from accidentally becoming a second
 * routing engine with different behaviour.
 */

import type { NavigationGraph } from "./floorplan-graph";

export type MobilityType =
  | "normal"
  | "wheelchair"
  | "elderly"
  | "temporary_injury"
  | "child"
  | "limited_mobility";

export interface Occupant {
  id: string;
  name: string;
  locationId: string;
  mobility: MobilityType;
}

export interface RouteResult {
  occupantId: string;
  occupantName: string;
  success: boolean;
  route: string[];
  routeCoords: Array<{ x: number; y: number }>;
  recommendedExit: string;
  distance: number;
  risk: string;
  usesStairs: boolean;
  usesRamp: boolean;
  accessibleRoute: boolean;
  status:
    | "waiting"
    | "evacuating"
    | "evacuated"
    | "blocked"
    | "no_route";
  message?: string;
}

export interface EvacuationPlan {
  fireRoomId: string;
  blockedNodes: string[];
  blockedEdges: Set<string>;
  routes: RouteResult[];
}

/** Keep the graph type import available to consumers that use the contracts. */
export type { NavigationGraph };
