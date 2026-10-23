output "app_url" {
  description = "Open the app here."
  value       = local.app_url
}

output "load_balancer_dns" {
  description = "Point your domain here (CNAME) if Terraform does not manage its DNS."
  value       = aws_lb.app.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}

output "usage_table" {
  value = aws_dynamodb_table.usage.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "cognito_user_pool_id" {
  description = "Add users here (see docs/aws.md)."
  value       = one(aws_cognito_user_pool.users[*].id)
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_ROLE_ARN variable in the GitHub repository."
  value       = one(aws_iam_role.github_deploy[*].arn)
}
