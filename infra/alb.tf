resource "aws_lb" "app" {
  name               = var.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  idle_timeout       = 3600 # Streamlit keeps a WebSocket open per browser tab
}

resource "aws_lb_target_group" "app" {
  name                 = var.name
  port                 = 8501
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    path                = "/_stcore/health"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Streamlit keeps each user's session in one task, so a browser must stay on the same task.
  stickiness {
    type            = "lb_cookie"
    cookie_duration = 86400
    enabled         = true
  }
}

# Plain HTTP: serves the app when there is no domain, otherwise redirects to HTTPS.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.app.arn

    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

# HTTPS with login in front of the app (when a domain is set).
resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.cert_arn

  dynamic "default_action" {
    for_each = local.enable_login ? [1] : []
    content {
      type  = "authenticate-cognito"
      order = 1
      authenticate_cognito {
        user_pool_arn       = aws_cognito_user_pool.users[0].arn
        user_pool_client_id = aws_cognito_user_pool_client.app[0].id
        user_pool_domain    = aws_cognito_user_pool_domain.login[0].domain
        session_timeout     = 43200
      }
    }
  }

  default_action {
    type             = "forward"
    order            = local.enable_login ? 2 : 1
    target_group_arn = aws_lb_target_group.app.arn
  }
}
