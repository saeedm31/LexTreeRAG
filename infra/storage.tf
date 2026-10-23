# ── Container registry

resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ── Shared law cache (EFS): every task and user shares the same indexed laws ──

resource "aws_efs_file_system" "cache" {
  encrypted = true
  tags      = { Name = "${var.name}-cache" }
}

resource "aws_efs_mount_target" "cache" {
  count           = 2
  file_system_id  = aws_efs_file_system.cache.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "cache" {
  file_system_id = aws_efs_file_system.cache.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/lextreerag"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }
}

# ── Usage and budgets per user and team

resource "aws_dynamodb_table" "usage" {
  name         = "${var.name}-usage"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ── Logs

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = 30
}
