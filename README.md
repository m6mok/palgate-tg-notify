# PalGate TG Notify

Telegram notification service for PalGate access control system.

## Development

### Running Tests

```bash
make test
```

### Type Checking

```bash
make mypy
```

### Mock Server

The project includes a production-ready mock server for PalGate API development and testing.

See [`palgate_server/README.md`](palgate_server/README.md) for detailed documentation.

## Configuration

Create a `.env` file based on [`.env.example`](.env.example):

```bash
cp .env.example .env
```

## Project Structure

```tree
.
├── docker_compose.dev.yaml  # Docker Compose configuration
├── Dockerfile                # Main application Dockerfile
├── Dockerfile.mock           # Mock server Dockerfile
├── Makefile                  # Build and test commands
├── pyproject.toml            # Project dependencies
├── src/                      # Main application source
│   ├── main.py              # Application entry point
│   ├── models.py            # Data models
│   ├── formatter.py         # Message formatting
│   ├── constants.py         # Application constants
│   ├── config/              # Configuration module
│   │   ├── __init__.py
│   │   └── settings.py      # Settings and Environment enum
│   ├── handlers/            # Request/response handlers
│   │   ├── __init__.py
│   │   ├── http.py          # HTTP request handlers
│   │   ├── cache.py         # Cache handlers
│   │   └── broadcast.py     # Broadcast handlers
│   └── services/            # Business logic services
│       ├── __init__.py
│       ├── token_generator.py  # Token generation service
│       └── log_updater.py      # Log update service
├── palgate_server/          # Mock PalGate server
│   ├── mock_server.py       # Flask server
│   ├── models.py            # Pydantic models
│   ├── config.json          # Server configuration
│   ├── handlers/            # Database handlers
│   └── tests/               # Mock server tests
└── tests/                   # Main application tests
    ├── conftest.py          # Test fixtures
    ├── test_main.py         # Unit tests
    ├── test_integration.py  # Integration tests
    ├── test_formatter.py    # Formatter tests
    └── test_models.py       # Model tests
```

## Architecture

The application follows a layered architecture with clear separation of concerns:

- **Config**: Application configuration and environment settings
- **Handlers**: HTTP request handling, caching, and broadcasting
- **Services**: Business logic including token generation and log updates
- **Main**: Application entry point and orchestration

## License

MIT
