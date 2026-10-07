locals {
  lambda_source_dir = "${path.module}/files"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------

data "archive_file" "ticketer" {
  type        = "zip"
  source_file = "${local.lambda_source_dir}/ado_ticketer.py"
  output_path = "${path.module}/.build/ado_ticketer.zip"
}

resource "aws_iam_role" "ticketer" {
  name = "${var.lambda_function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "ticketer" {
  name = "${var.lambda_function_name}-policy"
  role = aws_iam_role.ticketer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Same enrichment the Teams notifier does - service/env/team/runbook
        # tags go into the ticket description. ListTagsForResource has no
        # resource-level ARN support, hence "*".
        Sid      = "CloudWatchAlarmMetadata"
        Effect   = "Allow"
        Action   = ["cloudwatch:ListTagsForResource", "cloudwatch:DescribeAlarms"]
        Resource = "*"
      },
      {
        Sid      = "ReadAdoPat"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [var.ado_pat_secret_arn]
      },
      {
        Sid      = "LambdaLogging"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

resource "aws_lambda_function" "ticketer" {
  function_name = var.lambda_function_name
  role          = aws_iam_role.ticketer.arn
  handler       = "ado_ticketer.handler"
  runtime       = "python3.12"
  # 30s, not the Teams notifier's 15s: a cold invocation makes up to three
  # sequential HTTPS round trips to dev.azure.com (WIQL -> create/comment),
  # plus a Secrets Manager fetch.
  timeout     = 30
  memory_size = 128

  # Deliberately NOT in a VPC: it needs outbound internet to dev.azure.com,
  # and a private-subnet deployment needs its own NAT path for that - not
  # assumed here.

  filename         = data.archive_file.ticketer.output_path
  source_code_hash = data.archive_file.ticketer.output_base64sha256

  environment {
    variables = {
      ADO_ORG_URL               = var.ado_org_url
      ADO_PROJECT               = var.ado_project
      ADO_API_VERSION           = var.ado_api_version
      ADO_WORK_ITEM_TYPE        = var.ado_work_item_type
      ADO_PARENT_EPIC_ID        = var.ado_parent_epic_id
      ADO_AREA_PATH             = var.ado_area_path
      ADO_ITERATION_PATH        = var.ado_iteration_path
      ADO_PAT_SECRET_ARN        = var.ado_pat_secret_arn
      ADO_OPEN_STATE_EXCLUSIONS = var.ado_open_state_exclusions
      ACCOUNT_LABEL             = var.account_label
      ACCOUNT_ID                = data.aws_caller_identity.current.account_id
      AWS_CONSOLE_REGION        = data.aws_region.current.name
      DRY_RUN                   = var.dry_run ? "true" : "false"
    }
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# SNS -> Lambda wiring for the ticketed severities
# ---------------------------------------------------------------------------
# Topics themselves are owned by the sibling Teams-notifier module/project -
# subscriptions are independent resources, so owning them here doesn't
# conflict with that module's state.
resource "aws_sns_topic_subscription" "ticketer" {
  for_each = var.topic_arns

  topic_arn = each.value
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ticketer.arn
}

resource "aws_lambda_permission" "sns_invoke" {
  for_each = var.topic_arns

  statement_id  = "AllowSNS-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ticketer.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = each.value
}

# ---------------------------------------------------------------------------
# Self-monitoring
# ---------------------------------------------------------------------------
# If the ticketer itself fails, the alarms subscribed to var.topic_arns are
# going nowhere at all - they no longer fall back to Teams once this module
# is wired up. MUST route to var.critical_topic_arn, never one of
# var.topic_arns: routing this failure alarm through a topic the ticketer
# itself subscribes to creates a feedback loop (ADO down -> ticketer errors
# -> alarm -> ticketer -> errors).
resource "aws_cloudwatch_metric_alarm" "ticketer_errors" {
  alarm_name          = "ado-ticketer-errors-${var.account_label}"
  alarm_description   = "The ${var.account_label} CloudWatch -> ADO ticket automation Lambda is erroring. Affected severities are currently being dropped entirely (no Teams fallback). Check the PAT secret (${var.ado_pat_secret_arn}) for expiry first."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.ticketer.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  # No ok_actions - Teams/ADO are push-only channels, not an auto-resolving
  # system like PagerDuty, so a recovery message isn't actionable here
  # either; it was pure noise, including paging on-call outside hours for
  # something that had already self-resolved.
  alarm_actions = [var.critical_topic_arn]

  # Only severity is forced here - var.tags is expected to already carry the
  # right "service" tag (every account's project sets it from its own
  # vars.yaml), and overriding it with lambda_function_name would drift from
  # that convention wherever the Lambda's name isn't the bare service name
  # (e.g. "ms-qa-cloudwatch-ado-ticketer" vs. "cloudwatch-ado-ticketer").
  tags = merge(var.tags, {
    severity = "critical"
  })
}
