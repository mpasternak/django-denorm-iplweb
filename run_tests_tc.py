#!/usr/bin/env python
"""Run tests using testcontainers for PostgreSQL."""
import os
import subprocess
import sys

from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer


def main():
    with RedisContainer("redis:7-alpine") as rc:
        redis_url = (
            f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"
        )
        os.environ["DENORM_TEST_REDIS_URL"] = redis_url
        print(f"Redis running at {redis_url}")

        with PostgresContainer(
            "postgres:16",
            username="postgres",
            password="postgres",
            dbname="denorm_test",
        ) as pg:
            host = pg.get_container_host_ip()
            port = pg.get_exposed_port(5432)

            os.environ["DJANGO_SETTINGS_MODULE"] = (
                "test_denorm_project.settings_postgres"
            )
            os.environ["PGHOST"] = host
            os.environ["PGPORT"] = str(port)
            os.environ["PGUSER"] = "postgres"
            os.environ["PGPASSWORD"] = "postgres"

            # Override Django DB settings via env
            os.environ["DATABASE_HOST"] = host
            os.environ["DATABASE_PORT"] = str(port)
            os.environ["DATABASE_USER"] = "postgres"
            os.environ["DATABASE_PASSWORD"] = "postgres"

            print(f"PostgreSQL running at {host}:{port}")

            test_label = sys.argv[1] if len(sys.argv) > 1 else "test_app"
            result = subprocess.run(
                [sys.executable, "manage.py", "test", "--noinput", test_label],
                cwd="test_denorm_project",
            )
            sys.exit(result.returncode)


if __name__ == "__main__":
    main()
