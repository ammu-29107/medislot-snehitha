# MediSlot — CI/CD Pipeline Design

## 1. Objective

Use GitHub Actions and AWS CloudFormation to validate and deploy MediSlot infrastructure and application components through a repeatable, auditable pipeline.

This is the planned pipeline design, not a claim that the workflow or AWS resources are already deployed.

## 2. Source control and triggers

- Protected integration branch: `main`.
- Pull requests targeting `main`: run validation and tests; do not deploy production infrastructure from an unmerged pull request.
- Push/merge to `main`: run validation, prepare CloudFormation Change Sets, obtain the required approval, and deploy.
- Manual workflow dispatch may be used for controlled operational deployments, subject to the same validation and approval rules.

## 3. Authentication to AWS

Use GitHub Actions OIDC federation with an AWS IAM role. Do not store long-lived AWS access keys in GitHub.

GitHub repository secret:
- `AWS_ROLE_ARN`: ARN of the IAM role assumed through OIDC.

The IAM trust policy should restrict:
- the GitHub OIDC provider,
- the expected repository,
- the intended branch/environment subject,
- the `sts.amazonaws.com` audience.

The deployment role should follow least privilege and grant only the CloudFormation, S3 artifact, IAM pass-role, and service-specific permissions required by the declared templates. Avoid wildcard permissions where resource-level scoping is supported.

## 4. Configuration and secrets

A non-sensitive `config.json` may define:
- `AWSRegion`
- `Environment`
- `StackPrefix`
- `ProjectName`

Do not place passwords, JWT signing secrets, access keys, or other sensitive values in `config.json`.

Sensitive values such as database credentials and the JWT signing secret are supplied through SSM Parameter Store `SecureString` parameters. Configuration paths should be environment-scoped, for example `/medislot/{environment}/database/password` and `/medislot/{environment}/auth/jwt-signing-secret`.

## 5. Planned pipeline stages

1. **Checkout and environment setup**
   - Check out source.
   - Set up required language/runtime and tooling.

2. **Configuration validation**
   - Validate JSON syntax and required configuration keys.
   - Fail early on missing or malformed configuration.

3. **Static validation**
   - Run CloudFormation linting on templates.
   - Run formatting, dependency, and static checks selected for the repository.

4. **Automated tests**
   - Run unit tests and applicable contract/integration tests.
   - Do not require deployment credentials for validation-only pull-request checks unless a test explicitly needs an isolated test account.

5. **AWS OIDC authentication**
   - For deployment jobs, request GitHub OIDC token and assume the scoped AWS role.

6. **Package and upload artefacts**
   - Package Lambda/application artefacts and upload them to the designated deployment artefact location where required.

7. **Create CloudFormation Change Sets**
   - Create or update stacks in dependency order.
   - Inspect Change Set actions and reject unexpected destructive changes according to the project's review policy.

8. **Approval gate**
   - Use a protected GitHub Environment with required reviewers, or an explicitly documented operator approval step, before executing production-impacting Change Sets.
   - Approval must occur after the Change Set is available for review and before execution.
   - Validation-only PR workflows do not execute deployment Change Sets.

9. **Execute Change Sets**
   - Execute approved Change Sets in dependency order.
   - Wait for stack completion and fail the workflow if a stack reaches a failed terminal state.

10. **Post-deployment verification**
    - Run appropriate health/smoke checks.
    - Publish deployment outputs and logs without exposing secrets.

## 6. Deployment dependency order

The intended high-level order is:
1. Network foundation.
2. Data resources.
3. Authentication resources.
4. Application services (doctor, patient, appointment, availability, waitlist, health-camp).
5. Event/notification and monitoring resources.
6. Dashboard or operational presentation resources, where included in the deployment plan.

The final stack graph must follow explicit CloudFormation dependencies. A stack should not be deployed before its referenced network, data, or shared resources exist.

## 7. Change Set approval policy

- Pull requests: validate only; no automatic deployment.
- Merges to `main`: prepare Change Sets.
- Non-production environments may use an approved automated policy if explicitly configured.
- Production-impacting Change Sets require an approval gate before execution.
- Review destructive replacements/deletions and IAM changes carefully.
- Preserve CloudFormation rollback and recovery procedures in the deployment runbook.

Do not describe Change Sets as “reviewed and approved” without identifying the mechanism that captures the approval.

## 8. Failure handling

- Stop dependent deployment stages when a prerequisite stack fails.
- Retain workflow logs and CloudFormation events for diagnosis.
- Do not automatically retry destructive or ambiguous operations without checking stack state.
- Use CloudFormation rollback where appropriate; document manual recovery for unrecoverable states.
- Never print secrets into job output.

## 9. IAM permission design

The OIDC role requires only the permissions needed for:
- CloudFormation stack and Change Set operations.
- Upload/download of deployment artefacts.
- Passing explicitly named execution roles to CloudFormation/Lambda where required.
- Managing the specific SSM parameters needed by the deployment process.
- Service-specific resource creation/update actions declared in the templates.

The policy should be derived from the actual templates and deployment commands. Do not add unsupported actions speculatively; validate policy syntax and scope before deployment.
