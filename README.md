# EVAC-X

AI-powered building reconstruction and evacuation simulation system.

Upload building photos → AI generates a floor plan → Edit the plan → Simulate fire evacuation with animated routes.

## Prerequisites

- **Python 3.11+**
- **Node.js 18+**
- **GPU recommended** (YOLO-World runs faster with CUDA, but CPU works)

## Quick Start

Open **two terminals** — one for the backend, one for the frontend.

### 1. Backend (FastAPI)

```bash
cd backend

# Create virtual environment (first time only)
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies (first time only)
pip install -r requirements.txt

# Start the server
uvicorn app.main:app --reload --port 8000
```

Backend runs at **http://localhost:8000**

API docs at **http://localhost:8000/docs**

### 2. Frontend (Next.js)

```bash
cd frontend

# Install dependencies (first time only)
npm install

# Start the dev server
npm run dev
```

Frontend runs at **http://localhost:3000**

### 3. Open the app

Go to **http://localhost:3000** in your browser.

## Pages

| URL | Description |
|-----|-------------|
| `/` | Main dashboard — single image AI analysis & evacuation demo |
| `/reconstruct` | Photo-based floor plan reconstruction |
| `/reconstruct-3d` | Multi-photo reconstruction + full evacuation simulation |

## Routing safety model

- **NetworkX Dijkstra is the authoritative routing engine.**
- Hazards raise traversal cost or block edges; blocked edges are never selected.
- Wheelchair routes cannot traverse stairs and receive a preference for ramps.
- Elderly, temporary-injury, and child profiles penalise stair traversal.
- The frontend visualises backend routes; it does not independently choose evacuation paths.
- AI detections are treated as approximate evidence and should be reviewed before real-world use.

## How It Works

### Reconstruction Pipeline

```
Multiple building photos
        ↓
AI object detection (YOLO-World + OpenCV)
        ↓
Overlap detection & feature matching
        ↓
Camera pose estimation
        ↓
Multi-view landmark fusion
        ↓
Geometry reconstruction
        ↓
Editable 2D floor plan
```

### Evacuation Simulation

```
Floor plan (AI-generated or user-edited)
        ↓
Navigation graph generation
        ↓
Add occupants + select fire room
        ↓
NetworkX Dijkstra routing with mobility constraints
        ↓
Wheelchair users → never stairs; prefer accessible ramps
        ↓
Animated evacuation on 2D floor plan
```

## Key Features

- **Multi-photo reconstruction** — upload overlapping photos taken while walking through a building
- **Editable floor plan** — drag rooms, add doors/exits/ramps/stairs, undo/redo
- **AI object detection** — YOLO-World detects people, exits, doors, stairs, ramps
- **Fire simulation** — select a room, see it highlighted in red on the floor plan
- **Accessibility-aware routing** — wheelchair users are routed through ramps, never stairs
- **Animated evacuation** — occupants move along their routes with start/pause/reset controls
- **NetworkX Dijkstra routing** — hazard-aware, mobility-aware routing runs on the backend

## Project Structure

```
EVACX-ML-Final/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI application
│   │   ├── ai/                      # YOLO detector, preprocessing, floor plan generation
│   │   ├── reconstruction/          # Multi-photo reconstruction pipeline
│   │   ├── graph/                   # Graph building & routing (NetworkX)
│   │   ├── simulation/              # Evacuation simulation engine
│   │   ├── routing/                 # Graph adapter for routing
│   │   └── models/                  # Pydantic models
│   ├── requirements.txt
├── frontend/
│   ├── app/
│   │   ├── page.tsx                 # Main dashboard
│   │   ├── reconstruct/             # Photo-based reconstruction page
│   │   ├── reconstruct-3d/          # Multi-photo + evacuation simulation
│   │   └── api/                     # Next.js API routes (proxy to backend)
│   ├── components/
│   │   ├── floorplan/
│   │   │   └── FloorPlanViewer.tsx  # SVG floor plan renderer + editor
│   │   └── SimulationControls.tsx   # Evacuation animation controls
│   ├── lib/
│   │   ├── floorplan-graph.ts       # Floor-plan geometry helpers
│   │   └── evacuation-routing.ts    # Shared evacuation result types
│   └── package.json
```

## Environment Variables

### Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKEND_URL` | `http://127.0.0.1:8000` | Backend URL for internal API calls |

### Frontend

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_BACKEND_HTTP` | `http://127.0.0.1:8000` | Backend URL for client-side API calls |

Create a `.env.local` file in `frontend/` if you need to override:

```
NEXT_PUBLIC_BACKEND_HTTP=http://127.0.0.1:8000
```

## Troubleshooting

### Backend won't start

Make sure you're using the **virtual environment Python**, not the system Python:

```bash
cd backend
venv\Scripts\activate    # Windows
source venv/bin/activate  # macOS/Linux
uvicorn app.main:app --reload
```

### YOLO-World slow on first request

The first AI analysis loads the YOLO-World model (~100MB). Subsequent requests are faster. GPU acceleration is recommended.

### Frontend can't reach backend

Ensure the backend is running on port 8000 before using the frontend. The frontend proxies API calls to the backend.

### Port already in use

```bash
# Find and kill the process on port 8000
# Windows:
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# macOS/Linux:
lsof -ti:8000 | xargs kill -9
```
