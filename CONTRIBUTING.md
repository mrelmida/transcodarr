# Contributing to Transcodarr

Thanks for your interest in contributing! This guide will help you get started.

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL 16+
- FFmpeg (with libzimg for HDR tone-mapping)
- Docker & Docker Compose (for containerised development)

### Local Development Setup

```bash
# Clone the repo
git clone https://github.com/reedylab/transcodarr.git
cd transcodarr

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
pip install -e .

# Install test dependencies
pip install pytest

# Copy and configure environment
cp .env.example .env
# Edit .env with your local settings (Postgres credentials, paths, etc.)
```

### Running Tests

```bash
pytest tests/ -v
```

### Running Locally (Docker)

```bash
docker compose up -d --build
```

The UI will be available at `http://localhost:5025`.

## How to Contribute

### Reporting Bugs

Open a [GitHub Issue](https://github.com/reedylab/transcodarr/issues) with:

- Steps to reproduce
- Expected vs. actual behaviour
- Docker logs or tracebacks if available
- Your environment (OS, Docker version, FFmpeg version)

### Suggesting Features

Open an issue with the **feature request** label. Describe the use-case and why it would be useful.

### Submitting Pull Requests

1. Fork the repo and create a branch from `master`.
2. Make your changes — keep commits focused and atomic.
3. Add or update tests if your change affects behaviour.
4. Run `pytest tests/ -v` and ensure all tests pass.
5. Open a PR against `master` with a clear description of your changes.

### Code Style

- Follow existing patterns in the codebase.
- Use meaningful variable and function names.
- Keep functions focused — one function, one job.
- Add comments only where the logic isn't self-evident.

### Project Structure

```
transcodarr/
├── srt/transcodarr_core/   # Core library (config, database, ffmpeg, pipeline, subtitles)
├── web/                    # Flask/Gunicorn web app
│   ├── blueprints/         # API and UI route blueprints
│   ├── static/             # CSS, JS
│   └── templates/          # Jinja2 templates
├── tests/                  # Test suite
├── env_flag.py             # Stop-flag helper (reads/writes .env)
└── entrypoint.sh           # Docker entrypoint
```

## Security

If you discover a security vulnerability, please **do not** open a public issue. Instead, email the maintainers directly so it can be addressed before disclosure.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
