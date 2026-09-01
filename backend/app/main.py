from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from app.graph.builder import build_graph
from app.graph.routing import calculate_route
from PIL import Image
import io
import numpy as np

from app.ai.detector import EnvironmentDetector


app = FastAPI(
    title="AI Evacuation Twin"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],

    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


detector = EnvironmentDetector()


@app.get("/")
def root():

    return {
        "name": "AI Evacuation Twin",
        "status": "running"
    }


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...)
):

    contents = await file.read()

    image = Image.open(
        io.BytesIO(contents)
    ).convert("RGB")

    image_np = np.array(image)

    detections = detector.analyze(
        image_np
    )

    return {
        "success": True,
        "filename": file.filename,
        "detections": detections
    }


@app.post("/api/analyze-and-route")
async def analyze_and_route(
    file: UploadFile = File(...),
    mobility: str = "normal"
):

    contents = await file.read()

    image = Image.open(
        io.BytesIO(contents)
    ).convert("RGB")

    image_np = np.array(image)

    # -------------------------------
    # AI ANALYSIS
    # -------------------------------

    detections = detector.analyze(
        image_np
    )

    # -------------------------------
    # BUILD DIGITAL TWIN GRAPH
    # -------------------------------

    graph, exits = build_graph(
        detections
    )

    # -------------------------------
    # ROUTING
    # -------------------------------

    route = calculate_route(
        graph,
        start="current_position",
        mobility=mobility
    )

    # -------------------------------
    # ENVIRONMENT SUMMARY
    # -------------------------------

    people_count = sum(
        1
        for d in detections
        if d["type"] == "person"
    )

    exit_count = len(exits)

    hazards = [
        d for d in detections
        if d["type"] in {
            "fire",
            "smoke",
            "obstacle",
            "blocked passage"
        }
    ]

    # -------------------------------
    # GRAPH NODES
    # -------------------------------

    nodes = []

    for node_id, data in graph.nodes(
        data=True
    ):

        nodes.append({
            "id": node_id,
            "type": data.get(
                "type"
            ),
            "label": data.get(
                "label"
            ),
            "x": data.get(
                "x",
                0
            ),
            "y": data.get(
                "y",
                0
            ),
            "confidence": data.get(
                "confidence",
                1
            )
        })

    # -------------------------------
    # GRAPH EDGES
    # -------------------------------

    edges = []

    for source, target, data in graph.edges(
        data=True
    ):

        edges.append({
            "source": source,
            "target": target,
            "distance": round(
                data.get(
                    "distance",
                    0
                ),
                2
            ),
            "blocked": data.get(
                "blocked",
                False
            )
        })

    # -------------------------------
    # RESPONSE
    # -------------------------------

    return {
        "success": True,

        "environment": {
            "people": people_count,
            "exits": exit_count,
            "hazards": len(hazards),
            "detections": len(detections)
        },

        "detections": detections,

        "digital_twin": {
            "nodes": nodes,
            "edges": edges
        },

        "route": route
    }

from app.graph.demo_graph import create_demo_graph
@app.post("/api/demo/route")
async def demo_route(
    mobility: str = "normal"
):

    graph = create_demo_graph()

    route = calculate_route(
        graph,
        mobility=mobility
    )

    return {
        "success": True,
        "scenario": "normal",
        "mobility": mobility,
        "route": route
    }

@app.post("/api/demo/fire")
async def demo_fire(
    mobility: str = "normal"
):

    graph = create_demo_graph()

    # =====================================
    # FIRE BLOCKS EXIT A
    # =====================================

    graph["stairs"]["exit_a"][
        "hazard"
    ] = 10

    # =====================================
    # ADD FIRE TO GRAPH
    # =====================================

    graph.nodes["stairs"]["hazard"] = 1

    route = calculate_route(
        graph,
        mobility=mobility
    )

    return {
        "success": True,
        "scenario": "fire",
        "hazard": {
            "type": "fire",
            "location": "EXIT A / STAIRS",
            "severity": "HIGH"
        },
        "mobility": mobility,
        "route": route
    }