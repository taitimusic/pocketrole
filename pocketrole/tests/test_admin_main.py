"""tests/test_admin_main.py — hosted admin service 起動設定のテスト。"""

from __future__ import annotations

import os

import pytest

from admin.main import parse_args, resolve_settings


def test_resolve_settings_reads_env_defaults() -> None:
    args = parse_args([])
    settings = resolve_settings(
        args,
        env={
            "POCKETROLE_ADMIN_USER": "root-admin",
            "POCKETROLE_ADMIN_PASSWORD": "strong-pass",
            "POCKETROLE_ADMIN_PORT": "9797",
            "POCKETROLE_ADMIN_COOKIE_SECURE": "0",
        },
    )

    assert settings.admin_user == "root-admin"
    assert settings.admin_password == "strong-pass"
    assert settings.port == 9797
    assert settings.cookie_secure is False


def test_resolve_settings_cli_overrides_env() -> None:
    args = parse_args(
        [
            "--admin-user",
            "cli-admin",
            "--admin-password",
            "cli-pass",
            "--port",
            "8888",
            "--cookie-secure",
        ]
    )
    settings = resolve_settings(
        args,
        env={
            "POCKETROLE_ADMIN_USER": "env-admin",
            "POCKETROLE_ADMIN_PASSWORD": "env-pass",
            "POCKETROLE_ADMIN_PORT": "9797",
            "POCKETROLE_ADMIN_COOKIE_SECURE": "0",
        },
    )

    assert settings.admin_user == "cli-admin"
    assert settings.admin_password == "cli-pass"
    assert settings.port == 8888
    assert settings.cookie_secure is True


def test_resolve_settings_rejects_default_credentials() -> None:
    args = parse_args([])
    with pytest.raises(ValueError, match="admin credentials"):
        resolve_settings(
            args,
            env={
                "POCKETROLE_ADMIN_USER": "admin",
                "POCKETROLE_ADMIN_PASSWORD": "admin",
            },
        )
