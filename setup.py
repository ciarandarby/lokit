import json
from pathlib import Path
from typing import cast

from setuptools import setup
from setuptools.command.sdist import sdist


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        path = Path(base_dir) / "src/lokit_office_runtime/runtime.json"
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Office runtime metadata must be a JSON object")
        metadata = cast("dict[str, object]", value)
        for key in ("rid", "build_commit", "sha256"):
            metadata[key] = ""
        path.unlink()
        path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


setup(cmdclass={"sdist": SourceDistribution})
