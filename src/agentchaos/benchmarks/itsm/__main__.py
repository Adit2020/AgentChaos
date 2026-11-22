"""Launch a fresh fixture on loopback for local HTTP testing."""

import argparse
import json

import uvicorn

from .api import create_app
from .database import isolated_environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    with isolated_environment() as environment:
        token_file = environment.database.with_name("tokens.json")
        token_file.touch(mode=0o600, exist_ok=False)
        token_file.write_text(json.dumps(dict(environment.tokens)), encoding="utf-8")
        print(f"Fixture credentials file {token_file}", flush=True)
        uvicorn.run(
            create_app(environment),
            host="127.0.0.1",
            port=args.port,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
