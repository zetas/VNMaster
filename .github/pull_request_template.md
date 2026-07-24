## Summary

Describe the behavior changed and why.

## Verification

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check src tests`
- [ ] `uv run mypy src`
- [ ] No live credentials, cookies, private data, game archives, or save data were added
- [ ] Network/download behavior has focused tests using local mocks
- [ ] Security-sensitive behavior and documentation were updated where needed
