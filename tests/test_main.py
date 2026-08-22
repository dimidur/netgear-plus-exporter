"""Tests for the entry point's configuration reading.

The password is the one setting with two sources and a precedence between
them, and every way of getting it wrong has to name what to fix: a secrets
file that is missing or empty otherwise surfaces later as a rejected login,
which reports a configuration mistake as a bad password.

Every fault raises ConfigError so main() reports all of them the same way,
through the configured log format at ERROR with one exit code.
"""

from __future__ import annotations

import pytest

from netgear_plus_exporter.__main__ import ConfigError, _read_host, _read_password

ENV = "NETGEAR_EXPORTER_PASSWORD"
FILE_ENV = "NETGEAR_EXPORTER_PASSWORD_FILE"


@pytest.fixture(autouse=True)
def _clear_password_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv(FILE_ENV, raising=False)


def test_the_env_var_is_read(monkeypatch):
    # Arrange
    monkeypatch.setenv(ENV, "hunter2")

    # Act + Assert
    assert _read_password() == "hunter2"


def test_the_file_is_read_and_stripped(monkeypatch, tmp_path):
    """Docker secrets and Kubernetes both mount files with a trailing
    newline, which is not part of the password.
    """
    # Arrange
    secret = tmp_path / "password"
    secret.write_text("hunter2\n", encoding="utf-8")
    monkeypatch.setenv(FILE_ENV, str(secret))

    # Act + Assert
    assert _read_password() == "hunter2"


def test_the_file_wins_over_the_env_var(monkeypatch, tmp_path):
    """Documented precedence: the *_FILE form is the one that keeps the
    password out of docker inspect, so it cannot be shadowed by a stale env
    var left in a compose file.
    """
    # Arrange
    secret = tmp_path / "password"
    secret.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv(FILE_ENV, str(secret))
    monkeypatch.setenv(ENV, "from-env")

    # Act + Assert
    assert _read_password() == "from-file"


def test_a_missing_file_names_the_path(monkeypatch, tmp_path):
    # Arrange
    missing = tmp_path / "absent"
    monkeypatch.setenv(FILE_ENV, str(missing))

    # Act + Assert
    with pytest.raises(ConfigError) as caught:
        _read_password()

    assert str(missing) in str(caught.value)
    assert FILE_ENV in str(caught.value)


def test_an_empty_file_is_rejected_rather_than_passed_on(monkeypatch, tmp_path):
    """An empty password would reach the switch and come back as a rejected
    login, blaming the credentials for an empty mount.
    """
    # Arrange
    secret = tmp_path / "password"
    secret.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv(FILE_ENV, str(secret))

    # Act + Assert
    with pytest.raises(ConfigError) as caught:
        _read_password()

    assert "empty" in str(caught.value)
    assert str(secret) in str(caught.value)


def test_neither_source_set_names_both():
    # Arrange: the autouse fixture cleared both variables.

    # Act + Assert
    with pytest.raises(ConfigError) as caught:
        _read_password()

    assert ENV in str(caught.value)
    assert FILE_ENV in str(caught.value)


def test_a_missing_host_is_the_same_kind_of_failure(monkeypatch):
    """The host and the password are both required settings, so they fail the
    same way. Exiting from one and logging from the other put two halves of
    one fault class on different channels and different exit codes.
    """
    # Arrange
    monkeypatch.delenv("NETGEAR_EXPORTER_HOST", raising=False)

    # Act + Assert
    with pytest.raises(ConfigError) as caught:
        _read_host()

    assert "NETGEAR_EXPORTER_HOST" in str(caught.value)


def test_a_file_that_is_not_utf8_names_the_path(monkeypatch, tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError, so it needs its own
    place in the guard or it escapes as a traceback.
    """
    # Arrange
    secret = tmp_path / "password"
    secret.write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setenv(FILE_ENV, str(secret))

    # Act + Assert
    with pytest.raises(ConfigError) as caught:
        _read_password()

    assert str(secret) in str(caught.value)
