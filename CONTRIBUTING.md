# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Before you open a pull request

The project's own checks, in the order they catch the most:

* **Run the verify gate.** `make verify` runs backend ruff + pytest, infra ruff + pytest, frontend
  eslint + tsc + vitest + vite build, i18n key parity and the zh-CN punctuation check. A change is
  not ready until it passes. It does not touch AWS: the backend tests are hermetic.
* **Use `uv` for Python.** Run backend and infra commands from their own directory with `uv run`,
  never bare `python` / `pip`.
* **Keep every user-facing string translated.** Add the key to both
  `frontend/src/locales/en/common.json` and `frontend/src/locales/zh-CN/common.json`; Chinese copy
  uses full-width punctuation (`python3 scripts/i18n_zh_punct.py --fix` converts it).
* **Classify new routes.** Every backend route needs an entry in
  `backend/app/core/route_policy.py`; an unclassified route is refused at runtime.
* **Build AWS clients in one place.** Construct boto3 clients only through
  `backend/app/services/aws_clients.py`; a guard test fails on construction anywhere else.
* **Keep `AGENTS.md` in step with `CLAUDE.md`.** Their bodies are mirrored and a test checks it.
* **Read the architecture map first** for anything that spans modules:
  [docs/architecture.md](docs/architecture.md).

The `backend/scripts/e2e_*.py` scripts exercise real AWS and create billable resources in your
account. They are not part of the verify gate; run them only against an account intended for
evaluation, and let each one clean up after itself.


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
