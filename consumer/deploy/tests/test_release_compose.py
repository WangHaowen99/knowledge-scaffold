from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.release.yml"
DEPLOY_ROOT = COMPOSE_PATH.parent


def test_release_compose_has_no_legacy_config_mount_or_environment():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "./backend/config:" not in compose_text
    assert "LEGACY_SYSTEM_CONFIG_PATH" not in compose_text


def test_release_scripts_have_no_legacy_config_directory_plumbing():
    for script_name in (
        "backup.sh",
        "build_release.sh",
        "common.sh",
        "rollback.sh",
        "upgrade.sh",
    ):
        script_text = (DEPLOY_ROOT / script_name).read_text(encoding="utf-8")
        assert "backend/config" not in script_text
        assert "backend-config" not in script_text
