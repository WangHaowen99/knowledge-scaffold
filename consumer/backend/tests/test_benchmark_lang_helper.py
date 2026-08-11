import importlib
import os
import sys
from pathlib import Path


def test_benchmark_lang_loads_local_dotenv_into_environment(tmp_path, monkeypatch):
    helper_dir = tmp_path / "benchmarks"
    helper_dir.mkdir()
    helper_path = helper_dir / "lang.py"
    source_path = Path(__file__).resolve().parent / "benchmarks" / "lang.py"
    helper_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    (helper_dir / ".env").write_text(
        "LLM_API_KEY=from-dotenv\nLLM_BASE_URL=https://dotenv.example/v1\nLLM_MODEL_NAME=dotenv-model\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.syspath_prepend(str(helper_dir))
    sys.modules.pop("lang", None)

    helper = importlib.import_module("lang")

    assert helper.get_env("LLM_API_KEY") == "from-dotenv"
    assert os.environ["LLM_API_KEY"] == "from-dotenv"
    assert os.environ["LLM_BASE_URL"] == "https://dotenv.example/v1"
    assert os.environ["LLM_MODEL_NAME"] == "dotenv-model"
    assert os.environ["LLM_MODEL"] == "dotenv-model"
