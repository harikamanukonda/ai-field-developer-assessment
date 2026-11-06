"""Issue short-lived local demo JWTs. Secret is read from the environment."""
import argparse
import os
import time

import jwt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["viewer", "admin"], default="viewer")
    parser.add_argument("--tenant", default="demo-tenant")
    args = parser.parse_args()
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        parser.error("JWT_SECRET must be at least 32 characters")
    now = int(time.time())
    print(jwt.encode({"sub": args.tenant, "role": args.role, "iat": now, "exp": now + 3600,
                      "iss": "assessment", "aud": "assessment-gateway"}, secret, algorithm="HS256"))


if __name__ == "__main__":
    main()
