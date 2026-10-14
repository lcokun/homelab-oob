"""The X3000Z expects key_index + hex(AES-128-CBC(password)) using the fixed IV from its web UI."""
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

WATCHDOG_PATH = Path(__file__).parents[1] / "workstation" / "x3000z-watchdog.py"
RAND_KEY = "ab" * 16 + "0123456789abcdef0123456789abcdef"  # key_index (32 hex) + AES key (32 hex)
AES_BLOCK_BYTES = 16


def load_watchdog() -> ModuleType:
    spec = importlib.util.spec_from_file_location("x3000z_watchdog", WATCHDOG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("password", ["x", "dummy-password-123", "exactly16bytes!!"])
def test_encrypt_password_matches_web_ui_scheme(password: str) -> None:
    watchdog = load_watchdog()
    cipher = AES.new(bytes.fromhex(RAND_KEY[32:64]), AES.MODE_CBC, watchdog.AES_IV)
    expected = RAND_KEY[:32] + cipher.encrypt(pad(password.encode(), AES_BLOCK_BYTES)).hex()

    assert watchdog.encrypt_password(password, RAND_KEY) == expected
