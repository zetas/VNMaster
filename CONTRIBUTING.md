# Contributing

VNMaster targets macOS and Python 3.12 or newer. Install `uv`, clone the
repository, and prepare the development environment:

```bash
uv sync --extra dev
```

Before opening a pull request, run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv build
```

Tests must be deterministic and offline by default. Use local HTTP transports,
temporary directories, synthetic metadata, and fake credentials. Never commit
API keys, cookies, Discord webhook URLs, private databases, real save files,
game archives, or personally identifying fixtures.

Changes to downloading, redirect handling, archive extraction, credential
storage, add-on merging, or filesystem publication need focused failure-path
tests. Preserve transactional staging and explicit user confirmation.

Keep commits focused and use plain-language conventional commit messages where
practical, such as `fix(downloads): reject private redirect targets`.
