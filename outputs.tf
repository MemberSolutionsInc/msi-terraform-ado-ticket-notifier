output "lambda_function_name" {
  description = "Name of the ADO ticketer Lambda function."
  value       = aws_lambda_function.ticketer.function_name
}

output "lambda_function_arn" {
  description = "ARN of the ADO ticketer Lambda function."
  value       = aws_lambda_function.ticketer.arn
}

output "ticketer_errors_alarm_name" {
  description = "Name of the self-monitoring CloudWatch alarm on the ticketer Lambda's own Errors metric."
  value       = aws_cloudwatch_metric_alarm.ticketer_errors.alarm_name
}
