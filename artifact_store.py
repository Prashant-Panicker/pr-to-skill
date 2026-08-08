"""Local durable artifact storage adapter."""

import json
import os
import tempfile

class LocalArtifactStore:
    def __init__(self, output_dir: str):
        self._output_dir = output_dir

    def read_json(self, name: str):
        path = os.path.join(self._output_dir, name)
        if not os.path.exists(path):
            return None
        with open(path) as source:
            return json.load(source)

    def write_text(self, name: str, content: str) -> str:
        path = os.path.join(self._output_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=os.path.dirname(path), text=True
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
            os.replace(temporary_path, path)
        except Exception:
            os.unlink(temporary_path)
            raise
        return path