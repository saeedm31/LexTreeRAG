# Deploy on AWS

This branch (`aws`) runs LexTreeRAG inside your own AWS account, in the EU:

- **Claude through Amazon Bedrock**: no Anthropic key. The app's AWS role is allowed to call the models,
  and requests go to Bedrock in Stockholm (`eu-north-1`), so prompts and answers stay in the EU.
- **Shared law cache**: all users and app copies share one cache (Amazon EFS), so each law is indexed once.
- **Spend per person and team**: kept on the server (Amazon DynamoDB). Each team has a monthly budget;
  AI answers stop when it is reached.
- **Login (optional)**: with a domain name, users sign in through Amazon Cognito before they reach the app.
- **One command to create, one to remove**: everything is in Terraform (`infra/`).

```
 Users ──HTTPS──► Load balancer ──► App containers (ECS Fargate, private subnets) ──► Amazon Bedrock (Claude)
 (your IP, or      + login (Cognito)        │            │                      └──► EUR-Lex (via NAT)
  login)                                    ▼            ▼
                                     Law cache (EFS)   Usage + budgets (DynamoDB)
```

**Cost while running:** very roughly €70–100 a month for the infrastructure (mostly the NAT gateway and the
load balancer), plus model calls. Run `terraform destroy` when you are not using it.

---

## 1. One-time account setup (AWS console)

1. Pick the region **Europe (Stockholm) – eu-north-1** (top right of the console).
2. Open **Amazon Bedrock → Model catalog** and open **Claude Opus 5.5** and **Claude Haiku 4.5**. If AWS asks
   for it, submit the short Anthropic use-case form once. The app also offers Sonnet 5.5 and Haiku 5.5.
3. Open **CloudShell** (the `>_` icon in the top bar). All commands below run there; no keys on your laptop.
4. Check that Claude answers from your account:

   ```bash
   pip install --user --quiet "anthropic[bedrock]"
   python3 - <<'EOF'
   from anthropic import AnthropicBedrockMantle
   c = AnthropicBedrockMantle(aws_region="eu-north-1")
   r = c.messages.create(model="anthropic.claude-haiku-4-5", max_tokens=50,
                         messages=[{"role": "user", "content": "Say hello in five words."}])
   print(r.content[0].text, "|", r.usage.input_tokens, "tokens in,", r.usage.output_tokens, "out")
   EOF
   ```

   If this prints a hello, Bedrock is ready. An `AccessDenied` here means step 2 is not done yet.

## 2. Get the code and Terraform (CloudShell)

```bash
git clone -b aws https://github.com/saeedm31/LexTreeRAG.git
cd LexTreeRAG/infra

# Terraform (one time; CloudShell keeps your home folder)
mkdir -p ~/bin && cd ~/bin
curl -sLo tf.zip https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip
unzip -o tf.zip && rm tf.zip && cd - && export PATH=~/bin:$PATH
terraform -version
```

## 3. Settings

```bash
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```

- `allowed_cidrs`: who may open the app. For a first test, your own IP: open
  <https://checkip.amazonaws.com> **in your laptop's browser** and enter it as `["x.x.x.x/32"]`.
- `default_team`, `team_monthly_budget_eur`: the team budget in euros (AI answers stop at 100%).
- `alert_email`: an AWS budget alert for the whole account.
- Leave `domain_name` empty for now (no HTTPS, no login; access limited to your IP). See step 7.

## 4. Create everything

```bash
terraform init
terraform apply        # review the plan, type "yes"; takes about 10–15 minutes
```

At the end Terraform prints the outputs, including `app_url` and `github_deploy_role_arn`.
The app containers start failing until the image exists; step 5 fixes that.

## 5. Build and ship the app image

**Option A – GitHub Actions (recommended, no Docker needed).** The deploy role uses GitHub's OIDC
login, so no AWS keys are stored in GitHub.

1. In the GitHub repo: **Settings → Secrets and variables → Actions → Variables → New repository variable**:

   | Name | Value |
   |---|---|
   | `AWS_ROLE_ARN` | output `github_deploy_role_arn` |
   | `AWS_REGION` | `eu-north-1` |
   | `ECR_REPOSITORY` | `lextreerag` |
   | `ECS_CLUSTER` | `lextreerag` |
   | `ECS_SERVICE` | `lextreerag` |

2. **Actions → Deploy to AWS → Run workflow** (branch `aws`). From then on, every push to `aws` deploys.

**Option B – from CloudShell, if Docker is available there:**

```bash
cd ~/LexTreeRAG
REPO=$(terraform -chdir=infra output -raw ecr_repository_url)
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin ${REPO%/*}
docker build -t $REPO:latest . && docker push $REPO:latest
aws ecs update-service --cluster lextreerag --service lextreerag --force-new-deployment --region eu-north-1
```

## 6. Try it

1. Open `app_url` (`terraform output app_url`). The sidebar shows **Claude via Amazon Bedrock · eu-north-1**.
2. Ask a question, for example *When must a personal data breach be reported under the GDPR?*
   The first question for a law is slower: the law is fetched and indexed once.
3. Check the cost line under the answer and the **💶 Usage** tab (your spend and your team's spend).
4. In the console: **CloudWatch → Log groups → /ecs/lextreerag** for logs; **DynamoDB → lextreerag-usage**
   for the stored usage; **Billing → Bills** a day later for the Bedrock charges.

## 7. Add HTTPS and login (when you have a domain)

1. Put the domain in Route 53 (or use an existing hosted zone), then in `terraform.tfvars`:

   ```hcl
   domain_name     = "lex.example.com"
   route53_zone_id = "Z0123456789ABC"
   allowed_cidrs   = ["0.0.0.0/0"]      # safe now: everyone must sign in
   ```

2. `terraform apply` creates the certificate, the DNS record, the Cognito user pool and the login step.
3. Add users (no self sign-up; the team decides which budget they use):

   ```bash
   POOL=$(terraform output -raw cognito_user_pool_id)
   aws cognito-idp admin-create-user --user-pool-id $POOL --username ana@example.com \
     --user-attributes Name=email,Value=ana@example.com Name=email_verified,Value=true Name=custom:team,Value=legal
   ```

   Cognito emails a temporary password. A company can later connect its own Microsoft Entra ID, Google or
   Okta login to the same user pool, so staff use their normal account.

## 8. Stop paying

```bash
cd ~/LexTreeRAG/infra && terraform destroy
```

This removes everything, including the law cache and the usage table.

---

## What changed in the code (branch `aws`)

| File | Change |
|---|---|
| `pipeline/llm_client.py` | New. `LLM_PROVIDER=bedrock` → Claude via Bedrock (`AnthropicBedrockMantle`, AWS role, region `eu-north-1`). Maps the model names the pipeline uses to Bedrock IDs. |
| `pipeline/metering.py` | The token counter translates model names for Bedrock, so the 8 pipeline files are unchanged. |
| `pipeline/identity.py` | New. Reads the signed-in user and team from the load balancer's login header. |
| `pipeline/usage_store.py` | New. Usage per user and per team in DynamoDB; team budget check. |
| `pipeline/costs.py` | `PRICE_FACTOR` (Bedrock in-region endpoints cost 10% more). |
| `app.py` | AWS mode: no key field, no demo mode, Gemini off (it would send questions outside AWS), answer models available on Bedrock (default Claude Opus 5.5), user and team shown, usage saved on the server, team budget enforced. |
| `Dockerfile`, `.streamlit/config.toml`, `requirements-aws.txt` | The container. |
| `infra/` | Terraform: network, load balancer, ECS Fargate with auto scaling, EFS, DynamoDB, IAM roles, optional HTTPS + Cognito login, AWS budget alert, GitHub deploy role. |
| `.github/workflows/aws-deploy.yml`, `infra-check.yml` | Deploy on push to `aws`; check the Terraform and the container on every push. |

Without `LLM_PROVIDER=bedrock` the app behaves exactly like the public demo.

## Good to know

- **Models on Bedrock.** Bedrock's Messages-API endpoint serves Claude Opus 4.7 and newer plus Haiku 4.5, so on
  AWS the answer is written by Claude **Opus 5.5** by default (Opus 4.6 is only on the older Bedrock API). The
  small steps keep using Claude Haiku 4.5.
- **Price estimates** use the list prices with `PRICE_FACTOR=1.10`; the AWS bill is the real number.
- **Data.** Requests go to Bedrock in `eu-north-1`. AWS states that Bedrock does not share prompts and answers
  with the model provider. EUR-Lex is reached over the internet through the NAT gateway.
- **Login header.** The app reads the user from the load balancer's `x-amzn-oidc-data` header. The app accepts
  traffic only from the load balancer (security group), so the header cannot come from anywhere else.

## Next steps for real company use

- Keep Terraform state in S3 (instead of CloudShell) so several people can run it.
- Connect the company's login (Entra ID / Okta) to Cognito; map their groups to teams.
- Move the law cache to S3 and pre-index the most-used laws overnight.
- Per-team cost tags on Bedrock (projects / inference profiles), so finance sees spend per team in Cost Explorer.
- AWS WAF in front of the load balancer; private Bedrock endpoint (VPC endpoint) instead of the NAT route.
