from ultralytics import YOLOWorld
import numpy as np


class EnvironmentDetector:

    def __init__(self):

        print("Loading YOLO-World...")

        # Better current model for custom vocabulary
        self.model = YOLOWorld("yolov8s-worldv2.pt")

        # Use concrete visual descriptions.
        # These generally work better than abstract concepts.
        self.classes = [
            "person",
            "door",
            "doorway",
            "entrance",
            "exit door",
            "emergency exit",
            "exit sign",
            "stairs",
            "staircase",
            "stairway",
            "ramp",
            "wheelchair ramp",
            "elevator",
            "corridor",
            "hallway",
            "fire",
            "flames",
            "smoke",
            "obstacle",
            "blocked passage"
        ]

        self.model.set_classes(self.classes)

        print("YOLO-World ready.")

    def analyze(self, image):

        results = self.model.predict(
            image,
            conf=0.08,
            iou=0.45,
            imgsz=1280,
            verbose=False
        )

        detections = []

        result = results[0]

        if result.boxes is None:
            return detections

        for box in result.boxes:

            xyxy = box.xyxy[0].cpu().numpy()

            confidence = float(
                box.conf[0].cpu().numpy()
            )

            class_id = int(
                box.cls[0].cpu().numpy()
            )

            # IMPORTANT:
            # Use the model's actual class mapping.
            class_name = self.classes[class_id]

            x1, y1, x2, y2 = map(
                int,
                xyxy
            )

            detections.append({
                "type": class_name,
                "confidence": round(
                    confidence,
                    3
                ),
                "bbox": [
                    x1,
                    y1,
                    x2,
                    y2
                ]
            })

        return detections