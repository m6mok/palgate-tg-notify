# PalGate TG Notify

Telegram notification service for PalGate access control system.

## Development

### Running Tests

```bash
make test
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
│   └── formatter.py         # Message formatting
├── palgate_server/          # Mock PalGate server
│   ├── mock_server.py       # Flask server
│   ├── models.py            # Pydantic models
│   ├── config.json          # Server configuration
│   ├── handlers/            # Database handlers
│   └── tests/               # Mock server tests
└── tests/                   # Main application tests
```

## License

MIT
