import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from inference import ReleaseBackend


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class ModelArtifactIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "adapter_config.json").write_text("{}", encoding="utf-8")
        (self.root / "adapter_model.safetensors").write_bytes(b"fixture weights")
        (self.root / "tokenizer.json").write_text('{"tokens":[]}', encoding="utf-8")
        (self.root / "tokenizer_config.json").write_text("{}", encoding="utf-8")
        (self.root / "chat_template.jinja").write_text("template", encoding="utf-8")
        self.file_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.root.iterdir())
            if path.is_file()
        }
        tokenizer_hashes = {
            name: self.file_hashes[name]
            for name in ("tokenizer.json", "tokenizer_config.json")
        }
        runtime = {
            "tokenizer_sha256": hashlib.sha256(canonical(tokenizer_hashes)).hexdigest(),
            "template_sha256": self.file_hashes["chat_template.jinja"],
        }
        self.manifest = {
            "model_sha256": hashlib.sha256(canonical(self.file_hashes)).hexdigest(),
            "runtime": runtime,
            "model_source": {"template_file": "chat_template.jinja"},
            "model_file_hashes": self.file_hashes,
            "tokenizer_file_hashes": tokenizer_hashes,
        }

    def test_pinned_adapter_files_are_checked_before_loading(self):
        ReleaseBackend._verify_adapter(self.manifest, str(self.root))

    def test_modified_adapter_file_is_rejected(self):
        (self.root / "adapter_model.safetensors").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed after release"):
            ReleaseBackend._verify_adapter(self.manifest, str(self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
