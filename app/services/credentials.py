import os
from typing import Any


def resolve_connection(config: dict[str, Any]) -> dict[str, Any]:
    connection: dict[str, Any] = {}
    for key, value in config.items():
        if key.endswith("_env"):
            credential_name = key[:-4]
            secret_value = os.getenv(str(value))
            if secret_value:
                connection[credential_name] = secret_value
        else:
            connection[key] = value
    return connection
