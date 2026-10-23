variable "region" {
  description = "AWS region. eu-north-1 (Stockholm) keeps prompts and answers in the EU."
  type        = string
  default     = "eu-north-1"
}

variable "name" {
  description = "Name used for all resources."
  type        = string
  default     = "lextreerag"
}

variable "allowed_cidrs" {
  description = "Who may open the app, as IP ranges, e.g. [\"203.0.113.7/32\"]. Use [\"0.0.0.0/0\"] only together with login."
  type        = list(string)
}

# ── Domain, HTTPS and login (optional)

variable "domain_name" {
  description = "Domain for the app, e.g. lex.example.com. Empty = plain HTTP on the load balancer address, no login."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone of domain_name. When set, Terraform creates the certificate and the DNS record."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "Existing ACM certificate for domain_name (in this region). Leave empty to let Terraform create one."
  type        = string
  default     = ""
}

variable "enable_login" {
  description = "Require login (Amazon Cognito) in front of the app. Needs domain_name."
  type        = bool
  default     = true
}

# ── App settings

variable "image_tag" {
  description = "Container image tag to run."
  type        = string
  default     = "latest"
}

variable "cpu" {
  description = "Fargate CPU units per task (1024 = 1 vCPU)."
  type        = number
  default     = 1024
}

variable "memory" {
  description = "Fargate memory per task, in MB."
  type        = number
  default     = 2048
}

variable "min_tasks" {
  type    = number
  default = 1
}

variable "max_tasks" {
  type    = number
  default = 3
}

variable "default_answer_model" {
  description = "Model that writes the answers by default."
  type        = string
  default     = "claude-opus-5-5"
}

variable "default_team" {
  description = "Team for users without a custom:team attribute (and for everyone when login is off)."
  type        = string
  default     = "default"
}

variable "team_monthly_budget_eur" {
  description = "AI budget per team per month, in euros. AI answers stop when it is reached. 0 = no limit."
  type        = number
  default     = 50
}

variable "price_factor" {
  description = "Multiplier on list prices for the in-app estimate. Bedrock in-region endpoints cost 10% more."
  type        = number
  default     = 1.1
}

# ── Cost alert and CI/CD

variable "alert_email" {
  description = "Email for the AWS monthly budget alert. Empty = no alert."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "AWS budget for the whole account (all services), in US dollars."
  type        = number
  default     = 100
}

variable "github_repo" {
  description = "GitHub repository allowed to deploy (owner/name). Empty = no GitHub deploy role."
  type        = string
  default     = "saeedm31/LexTreeRAG"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if the account already has one."
  type        = bool
  default     = true
}
