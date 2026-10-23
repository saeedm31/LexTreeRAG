resource "aws_ecs_cluster" "main" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name         = "app"
    image        = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = 8501, protocol = "tcp" }]

    environment = [
      { name = "LLM_PROVIDER", value = "bedrock" },
      { name = "AWS_REGION", value = var.region },
      { name = "USAGE_TABLE", value = aws_dynamodb_table.usage.name },
      { name = "DEFAULT_TEAM", value = var.default_team },
      { name = "TEAM_MONTHLY_BUDGET_EUR", value = tostring(var.team_monthly_budget_eur) },
      { name = "PRICE_FACTOR", value = tostring(var.price_factor) },
      { name = "DEFAULT_ANSWER_MODEL", value = var.default_answer_model },
      { name = "CACHE_DIR", value = "/data/cache" },
      { name = "PDF_ARCHIVE_DIR", value = "/data/pdfs" },
    ]

    mountPoints = [{ sourceVolume = "data", containerPath = "/data", readOnly = false }]

    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "app"
      }
    }
  }])

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.cache.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.cache.id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "app" {
  name                              = var.name
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.app.arn
  desired_count                     = var.min_tasks
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 120

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8501
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count] # managed by auto scaling
  }

  depends_on = [aws_lb_listener.http, aws_efs_mount_target.cache]
}

# ── Scale out with load

resource "aws_appautoscaling_target" "app" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_tasks
  max_capacity       = var.max_tasks
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name}-cpu"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  policy_type        = "TargetTrackingScaling"

  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
