import json
import os
import subprocess
import sys
from pathlib import Path


def test_benchmark_lang_loads_local_dotenv_into_environment(tmp_path):
    helper_dir = tmp_path / "benchmarks"
    helper_dir.mkdir()
    helper_path = helper_dir / "lang.py"
    source_path = Path(__file__).resolve().parent / "benchmarks" / "lang.py"
    helper_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    (helper_dir / ".env").write_text(
        "LLM_API_KEY=your-api-key-here\nLLM_BASE_URL=https://service.example.net/v1\nLLM_MODEL_NAME=dotenv-model\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_MODEL_NAME"):
        env.pop(name, None)
    backend_dir = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(helper_dir), str(backend_dir)]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, os, lang; "
                "print(json.dumps({"
                "'api_key': lang.get_env('LLM_API_KEY'), "
                "'base_url': os.environ.get('LLM_BASE_URL'), "
                "'model_name': os.environ.get('LLM_MODEL_NAME'), "
                "'model': os.environ.get('LLM_MODEL')}))"
            ),
        ],
        cwd=helper_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "api_key": "your-api-key-here",
        "base_url": "https://service.example.net/v1",
        "model_name": "dotenv-model",
        "model": "dotenv-model",
    }
