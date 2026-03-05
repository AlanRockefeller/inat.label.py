# Contributing

Thanks for contributing to `inat.label.py`.

## Setup

1. Use Python 3.12+.
2. Install project dependencies from the README.
3. Install dev tools:

```bash
pip install pytest ruff
```

## Run checks

Run tests:

```bash
pytest -q
```

Run lint:

```bash
ruff check .
```

Optional formatting:

```bash
ruff format .
```

## Scope for first contributions

- Prefer small, reviewable pull requests.
- Add tests for logic-only functions first (parsing, sorting, normalization).
- Avoid changing output formatting unless the PR is specifically about output.
