# ── Certificate and DNS (when route53_zone_id is set)

resource "aws_acm_certificate" "app" {
  count             = local.create_cert ? 1 : 0
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert" {
  for_each = local.create_cert ? {
    for o in aws_acm_certificate.app[0].domain_validation_options : o.domain_name => o
  } : {}
  zone_id         = var.route53_zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "app" {
  count                   = local.create_cert ? 1 : 0
  certificate_arn         = aws_acm_certificate.app[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert : r.fqdn]
}

resource "aws_route53_record" "app" {
  count   = local.https && var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.app.dns_name
    zone_id                = aws_lb.app.zone_id
    evaluate_target_health = true
  }
}

# ── Login: Amazon Cognito user pool
# Users are added by an admin (no self sign-up). A company can later connect its own
# Microsoft Entra ID / Google / Okta login to this pool as an identity provider.

resource "aws_cognito_user_pool" "users" {
  count                    = local.enable_login ? 1 : 0
  name                     = var.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  schema {
    name                = "team"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }
}

resource "aws_cognito_user_pool_domain" "login" {
  count        = local.enable_login ? 1 : 0
  domain       = "${var.name}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.users[0].id
}

resource "aws_cognito_user_pool_client" "app" {
  count                                = local.enable_login ? 1 : 0
  name                                 = var.name
  user_pool_id                         = aws_cognito_user_pool.users[0].id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${var.domain_name}/oauth2/idpresponse"]
}

# ── Cost alert for the whole AWS account

resource "aws_budgets_budget" "monthly" {
  count        = var.alert_email != "" ? 1 : 0
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
