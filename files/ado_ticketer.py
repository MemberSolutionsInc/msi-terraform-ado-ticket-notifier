"""
SNS -> Azure DevOps ticket automation for CloudWatch Alarms (warning/info only).

Triggered by an SNS subscription on the warning and info severity topics
created by the sibling cloudwatch-sns-notifier project (the Teams Lambda
there is configured via notify_severities to no longer subscribe to those
two topics - see that project's main.tf). Critical alarms are untouched and
keep going to Teams as before.

For each alarm transitioning to ALARM, this Lambda:
  1. Builds an idempotency key from the AWS account ID + the verbatim
     AlarmName (not parsed/decomposed - alarm-naming conventions are
     inconsistent across this repo, but within one account a name is
     unique per resource+metric+tier by construction, so the raw string is
     already a reliable match key).
  2. Queries Azure DevOps (WIQL) for an existing, non-closed Product
     Backlog Item tagged with that key under Epic #4985
     ("Observability & Alerting").
  3. If found: posts a comment noting the alarm fired again.
     If not found: creates a new, unassigned PBI under that epic.

OK and INSUFFICIENT_DATA transitions are ignored entirely - this tracks
recurring issues, not every state change. This has to be done in code:
CloudWatch alarm SNS notifications carry no message attributes, so an SNS
subscription filter policy can't see NewStateValue.

Deliberately dependency-free (stdlib only: json, os, time, base64, hashlib,
html, urllib.request, plus boto3 provided by the Lambda runtime).
"""
import base64
import hashlib
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

cloudwatch = boto3.client("cloudwatch")
secretsmanager = boto3.client("secretsmanager")

ADO_ORG_URL = os.environ.get("ADO_ORG_URL", "https://dev.azure.com/membersolutionsinc")
ADO_PROJECT = os.environ.get("ADO_PROJECT", "DevOps")
ADO_API_VERSION = os.environ.get("ADO_API_VERSION", "7.1")
ADO_WORK_ITEM_TYPE = os.environ.get("ADO_WORK_ITEM_TYPE", "Product Backlog Item")
ADO_PARENT_EPIC_ID = os.environ.get("ADO_PARENT_EPIC_ID", "")
ADO_AREA_PATH = os.environ.get("ADO_AREA_PATH", "DevOps")
ADO_ITERATION_PATH = os.environ.get("ADO_ITERATION_PATH", "DevOps")
ADO_PAT_SECRET_ARN = os.environ.get("ADO_PAT_SECRET_ARN", "")
ADO_OPEN_STATE_EXCLUSIONS = os.environ.get("ADO_OPEN_STATE_EXCLUSIONS", "Done,Removed")
ADO_TIMEOUT_SECONDS = 10

ACCOUNT_LABEL = os.environ.get("ACCOUNT_LABEL", "unknown")
ACCOUNT_ID_ENV = os.environ.get("ACCOUNT_ID", "")
AWS_CONSOLE_REGION = os.environ.get("AWS_CONSOLE_REGION", "us-east-1")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

ALERT_KEY_PREFIX = "alert-key"

# In-memory cache for the ADO PAT, so a burst of alarms firing together
# doesn't hit Secrets Manager once per invocation. Same TTL/behavior as the
# Teams notifier's secret cache.
_SECRET_CACHE_TTL_SECONDS = 300
_secret_cache = {}


# ---------------------------------------------------------------------------
# Reused verbatim from the sibling sns-teams-notifier Lambda (notifier.py) -
# kept byte-identical so the two Lambdas never disagree about the same
# alarm's tags/severity.
# ---------------------------------------------------------------------------

def get_alarm_tags(alarm_arn):
    """Look up an alarm's tags via CloudWatch, returning a plain dict.

    Returns an empty dict (rather than raising) if the lookup fails, so a
    tagging problem degrades to a less-informative ticket instead of a
    dropped one.
    """
    if not alarm_arn:
        return {}
    try:
        response = cloudwatch.list_tags_for_resource(ResourceARN=alarm_arn)
        return {tag["Key"]: tag["Value"] for tag in response.get("Tags", [])}
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment
        print(f"WARNING: failed to fetch tags for {alarm_arn}: {exc}")
        return {}


def get_cached_secret(secret_arn):
    """Fetch a Secrets Manager secret's string value, cached in-memory.

    Returns None (rather than raising) on failure.
    """
    now = time.monotonic()
    cached = _secret_cache.get(secret_arn)
    if cached and (now - cached["fetched_at"]) < _SECRET_CACHE_TTL_SECONDS:
        return cached["value"]

    try:
        response = secretsmanager.get_secret_value(SecretId=secret_arn)
        # Stripped defensively: a secret set via `put-secret-value` with a
        # trailing newline (easy to do by accident) silently corrupts the
        # base64 Basic-auth token built from it - ADO's edge then serves an
        # HTML sign-in page with HTTP 203 instead of a clean 401, which is
        # very easy to mistake for a networking problem.
        value = response["SecretString"].strip()
    except Exception as exc:  # noqa: BLE001 - caller logs the eventual None
        print(f"WARNING: failed to fetch secret {secret_arn}: {exc}")
        value = None

    _secret_cache[secret_arn] = {"value": value, "fetched_at": now}
    return value


def resolve_severity(tags, topic_arn):
    """Prefer an explicit "severity" tag; fall back to the SNS topic
    name's suffix (every severity topic in this org is named
    <prefix>-critical/-warning/-info by convention).
    """
    if tags.get("severity"):
        return tags["severity"]
    topic_name = (topic_arn or "").rsplit(":", 1)[-1]
    for sev in ("critical", "warning", "info"):
        if topic_name.endswith(f"-{sev}"):
            return sev
    return None


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def resolve_account_id(alarm_arn):
    """Account ID from the alarm ARN (arn:aws:cloudwatch:<region>:<account>:
    alarm:<name>), falling back to the ACCOUNT_ID env var if the ARN is
    missing or malformed.
    """
    if alarm_arn:
        parts = alarm_arn.split(":")
        if len(parts) >= 5 and parts[4]:
            return parts[4]
    return ACCOUNT_ID_ENV


def build_alert_key(account_id, alarm_name):
    """Stable, ADO-tag-safe idempotency key for an alarm.

    AlarmName is hashed rather than embedded: alarm names in this estate run
    long and can contain characters ADO tags treat as separators (',' and
    ';'). The hash is pure [0-9a-f], so it drops into a WIQL string literal
    with no escaping. 16 hex chars = 64 bits; collision risk across a few
    thousand alarms is negligible.
    """
    digest = hashlib.sha256(alarm_name.encode("utf-8")).hexdigest()[:16]
    return f"{ALERT_KEY_PREFIX}-{account_id}-{digest}"


# ---------------------------------------------------------------------------
# Azure DevOps REST API
# ---------------------------------------------------------------------------

def ado_request(url, pat, body=None, content_type="application/json", method=None):
    """Single urllib entry point for every ADO call.

    ADO PATs authenticate as HTTP Basic with an empty username
    (base64(":" + pat)) - not Bearer.

    Raises on 5xx/429 so the invocation fails and SNS retries the async
    Lambda delivery; returns None on 4xx (permanent failure - bad PAT, bad
    field, nonexistent parent) after logging ADO's response body, which
    carries the actual reason.
    """
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    try:
        with urllib.request.urlopen(req, timeout=ADO_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            if not raw or raw.strip() == b"":
                print(
                    f"WARNING: ADO {method or 'POST'} {url} -> HTTP {resp.status} "
                    f"with blank body (len={len(raw)}, repr={raw!r}); "
                    f"content-type={resp.getheader('Content-Type')!r} "
                    f"content-length={resp.getheader('Content-Length')!r}"
                )
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                print(
                    f"ERROR: ADO {method or 'POST'} {url} -> HTTP {resp.status} "
                    f"returned non-JSON body (len={len(raw)}): {raw[:300]!r}"
                )
                raise
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        print(f"ERROR: ADO {method or 'POST'} {url} -> HTTP {exc.code}: {detail}")
        if exc.code >= 500 or exc.code == 429:
            raise
        return None


def find_open_work_item(alert_key, pat):
    """Return the most-recently-changed non-closed work item tagged with
    alert_key, or None.
    """
    exclusions = [s.strip() for s in ADO_OPEN_STATE_EXCLUSIONS.split(",") if s.strip()]
    state_clause = " ".join(f"AND [System.State] <> '{s}'" for s in exclusions)
    wiql = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{ADO_PROJECT}' "
        f"AND [System.WorkItemType] = '{ADO_WORK_ITEM_TYPE}' "
        f"AND [System.Tags] CONTAINS '{alert_key}' "
        f"{state_clause} "
        "ORDER BY [System.ChangedDate] DESC"
    )
    url = f"{ADO_ORG_URL}/{ADO_PROJECT}/_apis/wit/wiql?$top=1&api-version={ADO_API_VERSION}"
    result = ado_request(url, pat, {"query": wiql})
    if not result:
        return None
    items = result.get("workItems", [])
    return items[0]["id"] if items else None


def build_description_html(alert_key, alarm, tags, severity):
    alarm_arn = alarm.get("AlarmArn") or ""
    trigger = alarm.get("Trigger", {}) or {}
    dimensions = trigger.get("Dimensions", []) or []
    dims_html = "".join(
        f"<li>{html.escape(str(d.get('name', '')))} = {html.escape(str(d.get('value', '')))}</li>"
        for d in dimensions
    ) or "<li>(none)</li>"

    console_link = ""
    if alarm_arn:
        console_url = (
            f"https://{AWS_CONSOLE_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_CONSOLE_REGION}#alarmsV2:alarm/{urllib.parse.quote(alarm.get('AlarmName', ''))}"
        )
        console_link = f'<p><a href="{html.escape(console_url)}">View alarm in CloudWatch console</a></p>'

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return (
        f"<p><b>Account:</b> {html.escape(ACCOUNT_LABEL)} ({html.escape(resolve_account_id(alarm_arn))})</p>"
        f"<p><b>Alarm:</b> {html.escape(alarm.get('AlarmName', ''))}</p>"
        f"<p><b>Description:</b> {html.escape(alarm.get('AlarmDescription') or '(none)')}</p>"
        f"<p><b>Severity:</b> {html.escape(severity or 'unknown')}</p>"
        f"<ul>"
        f"<li>service: {html.escape(tags.get('service', '(not tagged)'))}</li>"
        f"<li>env: {html.escape(tags.get('env', '(not tagged)'))}</li>"
        f"<li>team: {html.escape(tags.get('team', '(not tagged)'))}</li>"
        f"<li>runbook: {html.escape(tags.get('runbook', '(not tagged)'))}</li>"
        f"</ul>"
        f"<p><b>Metric:</b> {html.escape(trigger.get('Namespace', ''))}/{html.escape(trigger.get('MetricName', ''))}</p>"
        f"<p><b>Dimensions:</b></p><ul>{dims_html}</ul>"
        f"<p><b>Reason:</b> {html.escape(alarm.get('NewStateReason') or '(none provided)')}</p>"
        f"<p><b>First seen:</b> {now}</p>"
        f"{console_link}"
        f"<p><small>Tracking key: {html.escape(alert_key)}</small></p>"
    )


def create_work_item(alert_key, alarm, tags, severity, pat):
    title = f"[{ACCOUNT_LABEL}][{severity or 'unknown'}] {alarm.get('AlarmName', '')}"[:255]
    ado_tags = "; ".join([alert_key, "cloudwatch-alarm", ACCOUNT_LABEL, f"severity-{severity or 'unknown'}"])

    ops = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.AreaPath", "value": ADO_AREA_PATH},
        {"op": "add", "path": "/fields/System.IterationPath", "value": ADO_ITERATION_PATH},
        {"op": "add", "path": "/fields/System.Tags", "value": ado_tags},
        {"op": "add", "path": "/fields/System.Description", "value": build_description_html(alert_key, alarm, tags, severity)},
    ]
    # Deliberately no System.AssignedTo - these land unassigned.
    if ADO_PARENT_EPIC_ID:
        ops.append({
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"{ADO_ORG_URL}/{ADO_PROJECT}/_apis/wit/workitems/{ADO_PARENT_EPIC_ID}",
            },
        })

    url = (
        f"{ADO_ORG_URL}/{ADO_PROJECT}/_apis/wit/workitems/"
        f"${urllib.parse.quote(ADO_WORK_ITEM_TYPE)}?api-version={ADO_API_VERSION}"
    )
    return ado_request(url, pat, ops, content_type="application/json-patch+json")


def add_recurrence_comment(work_item_id, alarm, pat):
    """Record a re-fire on the existing ticket.

    Uses a System.History patch op rather than the /comments endpoint:
    /comments only exists on preview API versions, and this way reuses the
    same json-patch code path as create_work_item.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    reason = html.escape(alarm.get("NewStateReason") or "(none provided)")
    text = f"<b>Alarm fired again</b> at {now}.<br/>Reason: {reason}"
    url = f"{ADO_ORG_URL}/{ADO_PROJECT}/_apis/wit/workitems/{work_item_id}?api-version={ADO_API_VERSION}"
    return ado_request(
        url, pat, [{"op": "add", "path": "/fields/System.History", "value": text}],
        content_type="application/json-patch+json", method="PATCH",
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    results = []
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        topic_arn = sns.get("TopicArn")
        try:
            alarm = json.loads(sns.get("Message", "{}"))
        except ValueError:
            print(f"WARNING: could not parse SNS message as JSON: {sns.get('Message')!r}")
            continue

        # Only act on alarms firing. Warning/info topics also receive
        # ok_actions and INSUFFICIENT_DATA transitions; none of those should
        # open or touch a ticket - this tracks recurring issues, not every
        # state change.
        state = alarm.get("NewStateValue")
        if state != "ALARM":
            print(f"Skipping {alarm.get('AlarmName')}: NewStateValue={state}")
            results.append({"alarm": alarm.get("AlarmName"), "action": "skipped"})
            continue

        alarm_name = alarm.get("AlarmName")
        if not alarm_name:
            print("WARNING: SNS message has no AlarmName; cannot build an idempotency key")
            continue

        alarm_arn = alarm.get("AlarmArn")
        account_id = resolve_account_id(alarm_arn)
        tags = get_alarm_tags(alarm_arn)
        severity = resolve_severity(tags, topic_arn)
        alert_key = build_alert_key(account_id, alarm_name)

        pat = get_cached_secret(ADO_PAT_SECRET_ARN)
        if not pat:
            # Hard-fail rather than drop: without Teams as a fallback for
            # warning/info, a silent drop loses the alert entirely. Raising
            # lets SNS retry and trips the ticketer's own self-monitoring
            # alarm (see alarms.tf), which pages to the critical topic.
            raise RuntimeError(f"ADO PAT unavailable from {ADO_PAT_SECRET_ARN}")

        existing = find_open_work_item(alert_key, pat)

        if DRY_RUN:
            print(f"DRY_RUN: alarm={alarm_name} key={alert_key} existing={existing}")
            results.append({"alarm": alarm_name, "action": "dry-run", "existing": existing})
            continue

        if existing:
            add_recurrence_comment(existing, alarm, pat)
            print(f"Commented on existing work item {existing} for {alarm_name}")
            results.append({"alarm": alarm_name, "action": "commented", "work_item": existing})
        else:
            created = create_work_item(alert_key, alarm, tags, severity, pat)
            wid = (created or {}).get("id")
            print(f"Created work item {wid} for {alarm_name} ({alert_key})")
            results.append({"alarm": alarm_name, "action": "created", "work_item": wid})

    return {"results": results}
