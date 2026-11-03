"""Optional real-model backend for a manifest created from the HW 6+7 artifacts."""
import hashlib
import json
import threading
from pathlib import Path


class ReleaseBackend:
    backend_name = "transformers"

    def __init__(self, manifest):
        source = manifest.get("model_source")
        runtime = manifest.get("runtime")
        if not isinstance(source, dict) or not isinstance(runtime, dict):
            raise ValueError("A real-model manifest needs model_source and runtime")
        runtime_hash = hashlib.sha256(
            json.dumps(
                runtime,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if runtime_hash != manifest.get("runtime_sha256"):
            raise ValueError("Accepted runtime metadata does not match runtime_sha256")
        generation = runtime.get("generation")
        if not isinstance(generation, dict):
            raise ValueError("runtime.generation must be an object")
        system = generation.get("system")
        enable_thinking = generation.get("enable_thinking")
        do_sample = generation.get("do_sample")
        max_new_tokens = generation.get("max_new_tokens")
        seed = generation.get("seed")
        if (
            not isinstance(system, str)
            or not system.strip()
            or type(enable_thinking) is not bool
            or type(do_sample) is not bool
            or type(max_new_tokens) is not int
            or not 1 <= max_new_tokens <= 32
            or (do_sample and type(seed) is not int)
        ):
            raise ValueError("runtime.generation is invalid for this API")

        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_name = source.get("name")
        revision = source.get("revision")
        if not isinstance(model_name, str) or not model_name or not isinstance(revision, str) or not revision:
            raise ValueError("model_source needs a pinned model name and revision")
        if revision != runtime.get("base_revision"):
            raise ValueError("model_source revision differs from runtime.base_revision")
        self.loaded_id = manifest["model_id"]
        self.runtime_sha256 = manifest["runtime_sha256"]
        self.calls = 0
        self._lock = threading.Lock()
        self._torch = torch
        self._device = source.get("device", "cpu")
        dtype_name = runtime.get("dtype")
        dtype = getattr(torch, dtype_name, None)
        if dtype is None or not isinstance(dtype, torch.dtype):
            raise ValueError(f"Unsupported runtime dtype: {dtype_name}")

        kind = source.get("kind")
        adapter_path = None
        if kind == "adapter":
            adapter_path = self._find_adapter(source, manifest)
            self._verify_adapter(manifest, adapter_path)
            self.tokenizer = AutoTokenizer.from_pretrained(adapter_path, local_files_only=True)
        elif kind == "base":
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                revision=revision,
                local_files_only=True,
            )
        else:
            raise ValueError(f"Unsupported model_source kind: {kind}")

        if self._device == "auto":
            self._device = (
                "cuda" if torch.cuda.is_available()
                else "mps" if torch.backends.mps.is_available()
                else "cpu"
            )
        base = AutoModelForCausalLM.from_pretrained(
            model_name,
            revision=revision,
            dtype=dtype,
            local_files_only=True,
        )
        if adapter_path is not None:
            base = PeftModel.from_pretrained(base, adapter_path)
        self.model = base.to(self._device).eval()
        self._generation = generation
        self._system = system
        self._enable_thinking = enable_thinking
        self._do_sample = do_sample
        self._seed = seed
        self._max_new_tokens = max_new_tokens

    @staticmethod
    def _find_adapter(source, manifest):
        adapter_path = source.get("adapter_path")
        if isinstance(adapter_path, str) and Path(adapter_path).is_dir():
            return str(Path(adapter_path))

        run_id = source.get("run_id")
        tracking_uri = source.get("tracking_uri")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Adapter manifest needs adapter_path or run_id")
        from mlflow import MlflowClient
        from src.common import connect, load_params
        if tracking_uri:
            import mlflow
            mlflow.set_tracking_uri(tracking_uri)
        else:
            connect(load_params())
        client = MlflowClient()
        model_root = Path(client.download_artifacts(run_id, "model"))
        mlmodel_path = model_root / "MLmodel"
        if not mlmodel_path.is_file():
            raise FileNotFoundError(f"MLflow model metadata is missing: {mlmodel_path}")
        import yaml
        mlmodel = yaml.safe_load(mlmodel_path.read_text(encoding="utf-8"))
        relative = mlmodel["flavors"]["python_function"]["artifacts"]["adapter"]["path"].replace("\\", "/")
        adapter = (model_root / relative).resolve()
        if not adapter.is_relative_to(model_root.resolve()) or not adapter.is_dir():
            raise ValueError("MLflow adapter path is outside the downloaded model")
        return str(adapter)

    @staticmethod
    def _verify_adapter(manifest, adapter_path):
        root = Path(adapter_path).resolve()
        expected_files = manifest.get("model_file_hashes")
        expected_tokenizer = manifest.get("tokenizer_file_hashes")
        if not isinstance(expected_files, dict) or not expected_files:
            raise ValueError("Manifest is missing pinned model_file_hashes")
        actual_files = {}
        for relative, expected_hash in expected_files.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"Invalid adapter artifact path: {relative}")
            with path.open("rb") as stream:
                actual_files[Path(relative).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
            if actual_files[Path(relative).as_posix()] != expected_hash:
                raise ValueError(f"Adapter artifact changed after release: {relative}")
        model_hash = hashlib.sha256(
            json.dumps(actual_files, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
        if model_hash != manifest.get("model_sha256"):
            raise ValueError("Adapter model_sha256 does not match the files on disk")

        if not isinstance(expected_tokenizer, dict) or not expected_tokenizer:
            raise ValueError("Manifest is missing tokenizer_file_hashes")
        tokenizer_hash = hashlib.sha256(
            json.dumps(expected_tokenizer, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
        if tokenizer_hash != manifest["runtime"]["tokenizer_sha256"]:
            raise ValueError("runtime tokenizer_sha256 does not match its file list")
        for relative, expected_hash in expected_tokenizer.items():
            if expected_files.get(relative) != expected_hash:
                raise ValueError(f"Tokenizer file is not bound to the model hash: {relative}")

        template_name = manifest.get("model_source", {}).get("template_file")
        template_hash = manifest["runtime"]["template_sha256"]
        if not isinstance(template_name, str) or expected_files.get(template_name) != template_hash:
            raise ValueError("runtime template_sha256 does not match the pinned model files")

    def generate(self, prompt, limit):
        with self._lock:
            self.calls += 1
            if self._do_sample:
                self._torch.manual_seed(self._seed)
            messages = [
                {"role": "system", "content": self._system},
                {"role": "user", "content": prompt},
            ]
            rendered = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=self._enable_thinking,
            )
            batch = self.tokenizer(rendered, return_tensors="pt").to(self._device)
            with self._torch.inference_mode():
                output = self.model.generate(
                    **batch,
                    max_new_tokens=min(limit, self._max_new_tokens),
                    do_sample=self._do_sample,
                    pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
                )
            start = batch["input_ids"].shape[1]
            answer = self.tokenizer.decode(output[0, start:], skip_special_tokens=True)
            return {"answer": answer, "output_tokens": int(output.shape[1] - start)}
