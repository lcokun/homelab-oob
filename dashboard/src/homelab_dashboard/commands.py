"""Commands for the Yún, signed so that knowing the ntfy topic isn't enough to send one."""
import hashlib
import hmac


def sign_command(command: str, key: str, timestamp: int) -> str:
    """Format a command the way yun/ws-remote.py verifies it: "<command> <unix time> <hex hmac>"."""
    payload = f"{command} {timestamp}"
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload} {signature}"
