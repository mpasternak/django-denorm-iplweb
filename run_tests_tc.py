#!/usr/bin/env python
"""Run tests using testcontainers for PostgreSQL."""
import os
import subprocess
import sys

from testcontainers.postgres import PostgresContainer


def main():
    with PostgresContainer(
        "postgres:16", username="postgres", password="postgres", dbname="denorm_test"
    ) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)

        os.environ["DJANGO_SETTINGS_MODULE"] = "test_denorm_project.settings_postgres"
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
