# msi-terraform-ado-ticket-notifier

CloudWatch alarm → SNS → Lambda → Azure DevOps work item automation.

Noisy warning/info-severity alarms don't need to interrupt a Teams channel
every time they fire - they need a tracked, deduplicated ticket. This module
subscribes a Lambda to one or more SNS topics (normally the warning/info
topics created by the sibling
[msi-terraform-sns-teams-notifier](https://github.com/MemberSolutionsInc/msi-terraform-sns-teams-notifier)
module) and, for each alarm transitioning to `ALARM`:

1. Builds an idempotency key from the AWS account ID + the verbatim
   `AlarmName` (not parsed or decomposed - alarm-naming conventions differ
   across the org and aren't reliably machine-readable, but within one
   account a name is unique per resource+metric+tier by construction, so the
   raw string is already a dependable match key).
2. Queries Azure DevOps (WIQL) for an existing, non-closed work item tagged
   with that key under a configured parent epic.
3. If found: posts a comment noting the alarm fired again. If not found:
   creates a new, unassigned work item under that epic.

`OK` and `INSUFFICIENT_DATA` transitions are ignored entirely - this tracks
*recurring issues*, not every state change. CloudWatch alarm SNS
notifications carry no message attributes, so this has to be decided in
Lambda code rather than an SNS subscription filter policy.

## Delivery path

```
CloudWatch Alarm --(alarm_actions)--> SNS topic --(subscription)--> this Lambda --(WIQL / work item API)--> Azure DevOps
```

This module does **not** create the SNS topics - it only subscribes to
ones you pass in via `topic_arns`. Pair it with
`msi-terraform-sns-teams-notifier`'s `notify_severities` variable so the
same severities stop reaching Teams once this module is handling them:

```hcl
module "sns_teams_notifier" {
  source = "git::https://github.com/MemberSolutionsInc/msi-terraform-sns-teams-notifier.git?ref=v0.7.0"
  # severities keeps creating all three topics; notify_severities is what
  # actually stops Teams delivery for warning/info.
  severities        = ["critical", "warning", "info"]
  notify_severities = ["critical"]
  # ...
}

resource "aws_secretsmanager_secret" "ado_pat" {
  name = "${local.config.account_name}/cloudwatch-ado-ticketer/azure-devops-pat"
  tags = local.tags
}
# Populate out-of-band: aws secretsmanager put-secret-value --secret-id <name> --secret-string '<PAT>'
# Never with a trailing newline - see ado_ticketer.py's get_cached_secret
# comment for what that breaks.

module "ado_ticket_notifier" {
  source = "git::https://github.com/MemberSolutionsInc/msi-terraform-ado-ticket-notifier.git?ref=v1.0.0"

  account_label      = local.config.account_name
  lambda_function_name = "${local.config.account_name}-cloudwatch-ado-ticketer"
  ado_pat_secret_arn = aws_secretsmanager_secret.ado_pat.arn
  critical_topic_arn = module.sns_teams_notifier.sns_topic_arns["critical"]
  topic_arns = {
    warning = module.sns_teams_notifier.sns_topic_arns["warning"]
    info    = module.sns_teams_notifier.sns_topic_arns["info"]
  }

  tags = local.tags
}
```

## Rollout sequencing for a new account

1. Apply with `dry_run = true` while the Teams notifier's `notify_severities`
   is untouched (or doesn't yet exclude the severities you're ticketing) -
   zero coverage gap while you validate.
2. Create the PAT secret container, populate its value out-of-band (a
   **service account** PAT scoped to Work Items Read & Write - not a
   personal one, so the automation doesn't break when someone leaves).
   Confirm a dry-run invocation reaches ADO cleanly (check the Lambda's
   logs for a `DRY_RUN:` line, not an exception).
3. Set `dry_run = false`, apply, and verify end-to-end: a synthetic `ALARM`
   publish creates a ticket, a repeat publish comments instead of
   duplicating, an `OK`/`INSUFFICIENT_DATA` publish produces no ADO call at
   all, and closing the ticket (to a state in `ado_open_state_exclusions`)
   then re-firing opens a fresh one.
4. Only once that's confirmed: set the Teams notifier's `notify_severities`
   to exclude the now-ticketed severities and apply.

Before step 4, check `aws cloudwatch describe-alarms --state-value ALARM`
filtered to the severities you're cutting over - every one currently firing
opens a ticket on its next evaluation, so it's worth knowing that batch size
going in.

## Inputs

| Name | Description | Type | Default |
|---|---|---|---|
| `account_label` | Label used in ticket titles/description and the self-monitoring alarm's name. | `string` | n/a |
| `topic_arns` | Map of severity -> SNS topic ARN to subscribe this Lambda to. | `map(string)` | n/a |
| `critical_topic_arn` | SNS topic ARN the self-monitoring Errors alarm posts to - must be a topic a human watches, never one in `topic_arns`. | `string` | n/a |
| `ado_pat_secret_arn` | ARN of a caller-owned Secrets Manager secret holding the ADO PAT. | `string` | n/a |
| `lambda_function_name` | Name of the ticketer Lambda. | `string` | `"cloudwatch-ado-ticketer"` |
| `ado_org_url` | Azure DevOps organization URL. | `string` | `"https://dev.azure.com/membersolutionsinc"` |
| `ado_project` | Azure DevOps project. | `string` | `"DevOps"` |
| `ado_api_version` | ADO REST API version. | `string` | `"7.1"` |
| `ado_work_item_type` | Work item type created per alarm. | `string` | `"Product Backlog Item"` |
| `ado_parent_epic_id` | Work item ID of the parent epic every ticket links to. | `string` | `"4985"` |
| `ado_area_path` | AreaPath set on created work items. | `string` | `"DevOps"` |
| `ado_iteration_path` | IterationPath set on created work items. | `string` | `"DevOps"` |
| `ado_open_state_exclusions` | Comma-separated `System.State` values treated as closed. | `string` | `"Done,Removed"` |
| `dry_run` | Log intended ADO writes instead of making them. | `bool` | `true` |
| `tags` | Tags applied to the Lambda, its IAM role, and the self-monitoring alarm. | `map(string)` | `{}` |

## Outputs

| Name | Description |
|---|---|
| `lambda_function_name` | Name of the ticketer Lambda. |
| `lambda_function_arn` | ARN of the ticketer Lambda. |
| `ticketer_errors_alarm_name` | Name of the self-monitoring alarm. |

## Known gotchas

- **Trailing whitespace in the PAT secret breaks Basic auth silently.** ADO's
  edge responds with an HTML sign-in page (HTTP 203), not a clean 401, which
  looks exactly like a networking problem. The Lambda strips the secret
  value defensively, but set it without a trailing newline in the first
  place (`put-secret-value --secret-string "$PAT"` via a shell variable, not
  a heredoc or `cat` of a file that ends in a newline).
- **No auto-close.** A ticket never auto-closes when the alarm returns to
  `OK` - someone closes it manually once resolved.
- **Small race window.** Two near-simultaneous firings of the same alarm
  could both see "no existing ticket" if ADO's tag index lags a write by a
  second or two - worst case is one duplicate ticket. Not handled; a
  DynamoDB conditional-put dedup table is the clean fix if this proves to
  matter in practice.
