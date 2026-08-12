# Contributing to ZSvirt

Thank you for your interest in contributing to ZSvirt.

ZSvirt is an open infrastructure project for private cloud, server virtualization, and enterprise cloud management. We welcome contributions from users, developers, partners, and infrastructure teams.

Contributions may include bug reports, feature proposals, documentation improvements, tests, tools, integrations, and code changes.

## Before You Start

Before contributing, please read:

- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- [GOVERNANCE.md](GOVERNANCE.md)
- [SECURITY.md](SECURITY.md)
- [LICENSE](LICENSE)

By participating in this project, you are expected to follow the project code of conduct.

## Contributor License Agreement

All external contributors are required to sign the ZSvirt Contributor License Agreement (CLA) before their pull requests can be merged.

The CLA confirms that you have the right to submit the contribution and grants the project maintainer and its affiliated organizations the rights needed to use, modify, reproduce, distribute, sublicense, and relicense your contribution as part of ZSvirt.

This authorization allows the contribution to be used in ZSvirt open-source releases, commercial distributions, enterprise editions, hosted services, derivative works, and other project-maintained distributions.

If you are contributing on behalf of your employer or another organization, your employer or organization may be required to sign a Corporate CLA.

Pull requests from contributors who have not completed the required CLA check will not be merged.

## Ways to Contribute

You can contribute in many ways:

- Report bugs
- Request features
- Improve documentation
- Submit code changes
- Add or improve tests
- Improve installation or migration guides
- Share production experience and best practices
- Help review issues and pull requests

## Reporting Issues

Please use GitHub Issues to report bugs, request features, or ask project-related questions.

Before opening a new issue:

- Search existing issues to avoid duplicates.
- Use the appropriate issue template.
- Provide enough information for maintainers to understand and reproduce the problem.
- Include logs, screenshots, version information, and environment details when applicable.

Security vulnerabilities should not be reported through public issues. Please follow [SECURITY.md](SECURITY.md).

## Issue Types

Common issue types include:

- Bug Report
- Feature Request
- Question
- Engineering Task
- Documentation

Please choose the issue type that best matches your request.

## Pull Request Workflow

ZSvirt uses GitHub pull requests to manage code and documentation contributions.

Recommended workflow:

1. Fork the repository.
2. Create a new branch from the latest `main` branch.
3. Make your changes in the new branch.
4. Add or update tests when applicable.
5. Update documentation when behavior changes.
6. Run local checks before submitting.
7. Push your branch to your fork.
8. Open a pull request against the upstream repository.
9. Respond to review comments and update the pull request as needed.

Please keep each pull request focused on a single topic. Large changes should be discussed in an issue before implementation.

## Branch Naming

Use clear branch names that describe the purpose of the change.

Examples:

- `fix/vm-start-timeout`
- `feature/vmware-migration-check`
- `docs/update-installation-guide`
- `test/add-storage-unit-tests`

## Commit Messages

Use clear and descriptive commit messages.

Recommended format:

- `component: short description`

Examples:

- `vm: fix instance start timeout handling`
- `docs: update quick start guide`
- `storage: add validation for primary storage capacity`

Avoid vague messages such as:

- `fix bug`
- `update code`
- `change files`

## Pull Request Description

A pull request should describe:

- What changed
- Why the change is needed
- How the change was tested
- Related issues
- Compatibility or upgrade impact
- Screenshots for UI changes, if applicable

If the pull request fixes an issue, link it in the description.

Example:

- `Fixes #123`

## Tests

Please run relevant tests before submitting a pull request.

Depending on the repository, tests may include:

- Unit tests
- Integration tests
- Build checks
- Documentation checks
- Static analysis
- Installation or upgrade verification

If tests cannot be run locally, explain why in the pull request.

## Documentation

Documentation changes are welcome.

Please update documentation when:

- User-facing behavior changes
- Installation steps change
- Configuration options are added or removed
- APIs are added, changed, or deprecated
- Troubleshooting steps are discovered
- Migration or upgrade behavior changes

Documentation should be clear, accurate, and easy to follow.

## Code Review

Maintainers review pull requests for correctness, maintainability, compatibility, security, and project direction.

A pull request may require changes before it can be merged.

Please keep discussion respectful and focused on the technical topic.

## Compatibility

ZSvirt is infrastructure software. Compatibility matters.

When making changes, consider:

- Upgrade compatibility
- API compatibility
- Configuration compatibility
- Data migration impact
- Deployment and rollback impact
- Integration impact with other components

Breaking changes should be clearly documented and discussed before implementation.

## Security

Do not include secrets, private keys, credentials, customer data, or internal-only information in issues, pull requests, commits, logs, or screenshots.

If you find a security vulnerability, follow [SECURITY.md](SECURITY.md).

## License

By contributing to this project, you agree that your contributions will be licensed under the project license unless otherwise stated.

This project is currently planned to use the GNU General Public License v3.0. Some repositories or components may include third-party open-source software under different licenses. Please check the `LICENSE`, `NOTICE`, and related files in each repository for details.

Thank you for helping improve ZSvirt.
