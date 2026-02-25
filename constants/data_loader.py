import json
import os

def load_json(filename):
    base_path = os.path.dirname(__file__)
    file_path = os.path.join(base_path, filename)
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)
