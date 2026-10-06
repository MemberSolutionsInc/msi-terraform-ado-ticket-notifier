variable "lambda_function_name" {
  description = "Name of the ticketer Lambda function."
  type        = string
  default     = "cloudwatch-ado-ticketer"
}

variable "account_label" {
  description = "Short human-readable label for the AWS account this is deployed in (e.g. \"ms-qa\"). Used in ticket titles, the ticket description, and the self-monitoring alarm's name."
  type        = string
}

variable "topic_arns" {
  description = <<-EOT
    Map of severity -> SNS topic ARN this Lambda subscribes to. Only include
    the severities that should be ticketed instead of (or in addition to)
    posting to Teams - typically {warning = ..., info = ...} from the
    sibling msi-terraform-sns-teams-notifier module's sns_topic_arns output,
    with that module's own notify_severities set to exclude the same
    severities so they stop reaching Teams.

    The map's keys only matter for naming each aws_lambda_permission's
    statement_id uniquely - they don't need to match anything in the ticket
    data itself (severity is read from the alarm's own tags/topic ARN at
    invoke time, same as the Teams notifier).
  EOT
  type        = map(string)
}

variable "critical_topic_arn" {
  description = "SNS topic ARN this Lambda's own Errors alarm posts to. Must be a topic a human actually watches (normally the critical/Teams topic) - routing it through one of var.topic_arns would create a feedback loop if ADO itself is down."
  type        = string
}

variable "ado_pat_secret_arn" {
  description = <<-EOT
    ARN of a Secrets Manager secret whose value is an Azure DevOps Personal
    Access Token (Work Items: Read & Write), scoped to a service account.
    The caller owns this secret's lifecycle (creation, value, rotation) -
    this module only grants the Lambda's role read access to it, and the
    Lambda fetches it live at invoke time (cached in-memory for 5 minutes),
    so rotation needs no apply or redeploy.
  EOT
  type        = string
}

variable "ado_org_url" {
  description = "Azure DevOps organization URL."
  type        = string
  default     = "https://dev.azure.com/membersolutionsinc"
}

variable "ado_project" {
  description = "Azure DevOps project name tickets are created in."
  type        = string
  default     = "DevOps"
}

variable "ado_api_version" {
  description = "Azure DevOps REST API version."
  type        = string
  default     = "7.1"
}

variable "ado_work_item_type" {
  description = "Work item type created for each distinct alarm."
  type        = string
  default     = "Product Backlog Item"
}

variable "ado_parent_epic_id" {
  description = "ADO work item ID of the parent epic every ticket links to."
  type        = string
  default     = "4985"
}

variable "ado_area_path" {
  description = "AreaPath set on created work items."
  type        = string
  default     = "DevOps"
}

variable "ado_iteration_path" {
  description = "IterationPath set on created work items."
  type        = string
  default     = "DevOps"
}

variable "ado_open_state_exclusions" {
  description = "Comma-separated System.State values treated as closed - a ticket in one of these states no longer dedups a recurring alarm, so the next firing opens a new one instead of commenting on the stale one."
  type        = string
  default     = "Done,Removed"
}

variable "dry_run" {
  description = "When true, the Lambda logs the ticket/comment it would have created and makes no write call to ADO - the WIQL lookup still runs, so this validates ADO auth/connectivity without creating anything. Set false once a dry-run invocation looks right."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to the Lambda, its IAM role, and the self-monitoring alarm."
  type        = map(string)
  default     = {}
}
