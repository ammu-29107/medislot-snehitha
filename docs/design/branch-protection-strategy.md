# MediSlot — Branch Protection Strategy

## 1. Purpose

The `main` branch is the protected integration branch and the source branch for approved deployment workflows. This is consistent with the repository's branch naming decision; `main` serves the role sometimes called `master` in other repositories.

## 2. Pull request policy

Changes should be proposed through pull requests targeting `main`.

Recommended rules:
- Require a pull request before merging.
- Require at least one approving review.
- Dismiss stale approvals when new commits are pushed.
- Require conversation resolution before merge.
- Require the pull-request branch to be up to date with `main` before merge, where supported and appropriate.
- Require the designated CI validation/status checks to pass.
- Restrict direct pushes to `main` where repository permissions allow.

## 3. Protected actions

- Block force pushes to `main`.
- Block deletion of `main`.
- Restrict bypass permissions to explicitly authorised repository administrators/maintainers.
- Require status checks for configuration validation, CloudFormation linting, and automated tests that are part of the agreed CI contract.

## 4. Merge policy

Use the repository's selected merge strategy consistently. The strategy should preserve a readable history and ensure that only reviewed, validated changes enter `main`.

## 5. Deployment relationship

A successful merge to `main` may trigger the deployment workflow. Deployment still follows the CI/CD design, including Change Set creation and the required approval gate. Branch protection approval and deployment approval are separate controls; one does not automatically replace the other.

## 6. Configuration checklist

Configure the repository's branch ruleset or branch protection rule for `main` to require:
- Pull request.
- Minimum one approval.
- Required status checks.
- Conversation resolution.
- No force pushes.
- No branch deletion.

The exact status-check names should match the checks actually defined by the planned GitHub Actions workflow.
