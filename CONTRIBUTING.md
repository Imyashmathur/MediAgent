# Contributing to MediAgent

Thanks for helping improve MediAgent. The project is a development prototype,
not clinically validated or ready for real patient data. Use synthetic data in
all code, tests, screenshots, issues, and pull requests. Do not include personal
health information, credentials, or copies of actual medical reports.

## Getting started

1. Fork [MediAgent on GitHub](https://github.com/Imyashmathur/MediAgent),
   then clone your fork using the URL shown on its GitHub page.
2. Follow [Run locally](README.md#run-locally) to start the Docker Compose
   stack. Do not expose the local development configuration to the internet.
3. For substantial changes, describe the problem in an issue before investing
   in an implementation so maintainers and contributors can discuss scope.
4. Create a branch, make a focused change, and run relevant checks:

   ```sh
   git switch -c feature/my-change
   # Make and test your changes.
   git add <changed-files>
   git commit -m "Describe the change"
   git push -u origin feature/my-change
   ```

   Replace `<changed-files>` with the files you intend to contribute and review
   the staged changes before committing. `origin` should point to your fork.
5. Open a pull request from your fork's branch to `Imyashmathur/MediAgent`.
   Explain the problem, what changed, checks run, and remaining limitations.

## Checks

Run checks relevant to your changes. The backend suite uses Python 3.12:

```sh
cd backend
python -m pip install -r requirements.txt
python -m pytest
```

The frontend build and unit tests run from `frontend/`:

```sh
npm ci
npm test
npm run build
```

Service-backed, browser, MCP, and optional model tests have additional setup;
see [the README](README.md) for their commands and limitations. Some tests
create synthetic records in the configured local database. Never point them at
real patient data or an external service. In a pull request, state which checks
ran and which were skipped.

## AI and clinical changes

Do not claim that citations, an LLM judge, a range comparison, or a passing test
suite establishes clinical correctness. Document model and evaluation changes,
use synthetic examples, and preserve the ability to review results against their
sources. The [roadmap](docs/ROADMAP.md) lists areas that need work.

## Security and privacy concerns

Do not post exploitable vulnerability details, credentials, or personal data in
public issues or pull requests. Use GitHub's private vulnerability reporting
if it is enabled for the repository; otherwise contact a maintainer through an
appropriate private channel before sharing details. Do not assume that a
private reporting channel or response-time guarantee is available.

## Licensing

Contributions submitted for inclusion are made available under the repository's
[MIT License](LICENSE). Only submit work you have the right to contribute.
