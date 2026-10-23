terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      app        = var.name
      managed-by = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)

  # HTTPS (and so login) needs a domain name. Without one the app runs on plain HTTP, open only to allowed_cidrs.
  https        = var.domain_name != ""
  create_cert  = local.https && var.certificate_arn == "" && var.route53_zone_id != ""
  cert_arn     = var.certificate_arn != "" ? var.certificate_arn : try(aws_acm_certificate_validation.app[0].certificate_arn, "")
  enable_login = local.https && var.enable_login
  app_url      = local.https ? "https://${var.domain_name}" : "http://${aws_lb.app.dns_name}"
}
