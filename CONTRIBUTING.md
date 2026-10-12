# Contributing to agybot

Thank you for your interest in contributing to **agybot**! We welcome bug reports, feature requests, documentation improvements, and pull requests.

---

## 🛠️ Local Development Setup

1. **Fork and Clone**:
   ```bash
   git clone https://github.com/<your-username>/agybot.git
   cd agybot
   ```

2. **Install Dependencies**:
   agybot uses [`uv`](https://astral.sh/uv) for fast, deterministic dependency management:
   ```bash
   uv sync
   ```

3. **Run the Test Suite**:
   ```bash
   uv run pytest
   ```

4. **Lint and Format**:
   We use `ruff` for linting and code formatting:
   ```bash
   uv run ruff check
   uv run ruff format --check
   ```
   To auto-format:
   ```bash
   uv run ruff format
   ```

---

## 📋 Pull Request Guidelines

1. **Create a Feature Branch**:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. **Write Clean, Documented Code**:
   - Keep functions focused and well-typed.
   - Maintain compatibility with Python 3.12+.
3. **Add or Update Tests**:
   - Ensure all existing tests pass (`uv run pytest`).
   - Add unit tests for any new features or bug fixes under `tests/`.
4. **Submit Your PR**:
   - Provide a clear description of the problem solved and the approach taken.
   - Reference any related issues.

---

## 📄 License

By contributing to agybot, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
