# Contributing to LORE

We welcome contributions from the open-source community! Here is how you can help:

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/lore-agent/lore.git
   cd lore
   ```

2. **Set up a virtual environment**:
   ```bash
   python -m venv venv
   # On Windows:
   .\venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   pip install -e .
   ```

4. **Set up environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env to add your API keys
   ```

## Running Tests

We use `pytest` for unit testing. Make sure all tests pass before submitting a pull request:
```bash
pytest tests/
```

## Submitting Pull Requests

1. Fork the repository and create your branch from `main`.
2. Ensure your code follows the existing style and all tests pass.
3. Write clean, descriptive commit messages.
4. Submit a pull request with a detailed description of your changes.

## Code of Conduct

Please be respectful and constructive in all community interactions.
