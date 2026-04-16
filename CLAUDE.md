# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

django-denorm-iplweb is a Django application for automatic management of denormalized database fields. This is a PostgreSQL-only fork of the original django-denorm package, supporting Django 4.2+/5.2+ and Python 3.10+.

## Development Commands

### Running Tests
```bash
# Run all tests
python runtests.py postgres

# Run tests with coverage in test project
cd test_denorm_project && coverage run manage.py test --noinput --keepdb test_app

# Run specific test
python runtests.py postgres specific_test_name
```

### Code Quality
```bash
# Check code style (flake8 configured with max-line-length = 120)
flake8

# Run tox for multiple Python/Django versions
tox
```

### Version Management
```bash
# Bump version using bumpver (configured in pyproject.toml)
bumpver update
```

## Architecture Overview

### Core Components

**denorm/denorms.py**: Main denormalization logic with automatic dependency tracking and cache invalidation. Contains the core `flush()` and `rebuildall()` functions.

**denorm/fields.py**: Field decorators and classes:
- `@denormalized(DBField)`: Decorator to create auto-updating denormalized fields
- `@cached`: Decorator for caching expensive computations
- `CountField`, `CacheKeyField`: Specialized field types

**denorm/models.py**: Database models for tracking dirty instances and processing queues. Uses PostgreSQL LISTEN/NOTIFY for multi-instance coordination.

**denorm/dependencies.py**: Dependency tracking system using `depend_on_related()` to specify when denormalized fields should be recalculated.

### Management Commands

Located in `denorm/management/commands/`:
- `denorm_init`: Initialize denormalization triggers
- `denorm_rebuild`: Rebuild all denormalized fields
- `denorm_flush`: Flush pending updates
- `denorm_queue`: Manage processing queue
- `denorm_drop`: Remove denormalization triggers
- `denorm_rebuild_triggers`: Rebuild database triggers
- `denorm_sql`: Generate SQL for denormalization

### Database Support

**PostgreSQL-only**: Uses PostgreSQL-specific features like LISTEN/NOTIFY for real-time coordination between multiple application instances. Database triggers and custom SQL generation in `denorm/db/` directory.

## Test Structure

Test project located in `test_denorm_project/` with test models in `test_app/models.py`. Tests cover field types, dependency tracking, and multi-instance scenarios.

## Key Configuration

Settings in `denorm/conf/settings.py`. Legacy `DENORM_FLUSH_AFTER_REQUEST` setting is deprecated in favor of `DenormMiddleware` (defined in `denorm/middleware.py`).
