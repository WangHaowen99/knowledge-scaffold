from internal_access import internal
import base64
import threading


def _test_key() -> str:
    return base64.b64encode(b"0" * 32).decode("ascii")


def test_secret_codec_encrypts_with_envelope_and_decrypts_plaintext(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", _test_key())

    from utils.security.secrets import (
        decrypt_secret,
        encrypt_secret,
        is_encrypted_secret,
    )

    encrypted = encrypt_secret("secret-value", context="test.field")

    assert is_encrypted_secret(encrypted)
    assert encrypted.startswith("enc:v1:")
    assert "secret-value" not in encrypted
    assert decrypt_secret(encrypted, context="test.field") == "secret-value"
    assert decrypt_secret("legacy-secret", context="test.field") == "legacy-secret"


def test_key_file_first_create_is_atomic_under_concurrency(tmp_path, monkeypatch):
    import utils.security.secrets as secrets_module

    monkeypatch.delenv(secrets_module.SECRET_ENCRYPTION_KEY_ENV, raising=False)
    monkeypatch.setenv(secrets_module.PROJECT_ROOT_ENV, str(tmp_path))

    barrier = threading.Barrier(2)
    generated_keys = [b"1" * 32, b"2" * 32]
    generated_lock = threading.Lock()

    def fake_token_bytes(size: int) -> bytes:
        assert size == 32
        barrier.wait(timeout=2)
        with generated_lock:
            return generated_keys.pop(0)

    monkeypatch.setattr(secrets_module.secrets, "token_bytes", fake_token_bytes)

    results: list[bytes] = []
    errors: list[Exception] = []

    def load_key() -> None:
        try:
            results.append(internal(secrets_module).load_key())
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=load_key) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    stored = base64.b64decode((tmp_path / "config" / "secret.key").read_text(encoding="utf-8"), validate=True)
    assert stored == results[0]
