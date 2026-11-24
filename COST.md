# Cost & Cleanup

Cost estimate and teardown for the Book Recommender AWS deployment.

## Assumptions
- Region **us-east-1**, **on-demand** pricing, running **24/7** (730 hr/month).
- Figures are approximate list prices; "with free tier" notes the first-12-months AWS Free Tier where it applies.

## Deployed resources
- **2× EC2 `t3.micro`** (x86_64, Ubuntu 24.04) — `aliaksandr-shebushau-instance` (app) + `-instance-2` (db).
- **2× EBS 8 GB gp3** root volumes.
- **2× ECR repositories** — `gde-aliaksandrsh-br-be`, `gde-aliaksandrsh-br-db`.
- **CloudWatch Logs** group `/bookrec/app` (14-day retention).
- **IAM** role `BookRecAppRole` + instance profile `BookRecAppProfile` (no standing cost).
- **Bedrock Knowledge Base** `YQUPN7XN9Q` — *managed, pre-existing/shared course account* (no vector-store bill under this project's control).

## Monthly AWS cost (24/7)

| Resource | Spec | ~Monthly |
|---|---|---|
| EC2 app | `t3.micro` ($0.0104/hr) | $7.59 |
| EC2 db | `t3.micro` | $7.59 |
| Public IPv4 × 2 | in-use (billed since 2024) | $7.30 |
| EBS × 2 | 8 GB gp3 each (16 GB) | $1.28 |
| ECR | < 1 GB stored | ~$0.09 |
| CloudWatch Logs | low volume, 14-day retention | ~$0.20 |
| Bedrock KB | managed / shared | ~$0 standing |
| IAM role + profile | — | $0 |
| **AWS total** | | **≈ $24 / month** |

**With the EC2 Free Tier** (750 `t3.micro` hours/month free for the first 12 months, ~1 instance): **≈ $16 / month**.

## Non-AWS cost
- **Anthropic API (Claude Sonnet)** — the agent and bookshelf recommendations call Claude via `ANTHROPIC_API_KEY` (not Bedrock). Usage-based, roughly **$0.05–0.30 per assistant/recommendation request**. A demo is a few dollars; the per-user **1,000/day** request quota caps the worst case.
- **Bedrock Retrieve** (KB queries) — query-embedding cost is a fraction of a cent per call; negligible at demo volume.

## Key takeaways
- **~90 % of the cost is the two always-on EC2 instances + their public IPs** (~$22 of ~$24). Everything else is cents.
- **Biggest lever: stop the instances between demos.** A stopped instance bills only its EBS (~$1.28/month total) — no compute, no public-IP charge.
- A **single instance** (app + db together, or db on RDS free tier) would roughly halve compute + IP cost.

## Cleanup / teardown

**Pause (keep everything, stop charges):**
```bash
aws ec2 stop-instances --region us-east-1 --instance-ids i-0250b881be92b4e0d i-0069bc79b88311faf
```
Stopped instances bill only EBS (~$1.28/month). Public IPs change on restart.

**Full teardown (when the project is done):**
```bash
# 1. Terminate the instances (also deletes their EBS volumes, releases IPs)
aws ec2 terminate-instances --region us-east-1 --instance-ids i-0250b881be92b4e0d i-0069bc79b88311faf

# 2. Delete the ECR repositories
aws ecr delete-repository --repository-name gde-aliaksandrsh-br-be --region us-east-1 --force
aws ecr delete-repository --repository-name gde-aliaksandrsh-br-db --region us-east-1 --force

# 3. Delete the CloudWatch log group
aws logs delete-log-group --log-group-name /bookrec/app --region us-east-1

# 4. Remove the IAM role + instance profile
aws iam remove-role-from-instance-profile --instance-profile-name BookRecAppProfile --role-name BookRecAppRole
aws iam delete-instance-profile --instance-profile-name BookRecAppProfile
aws iam delete-role-policy --role-name BookRecAppRole --policy-name BedrockRetrieve
aws iam delete-role-policy --role-name BookRecAppRole --policy-name CloudWatchLogsWrite
aws iam detach-role-policy --role-name BookRecAppRole --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam delete-role --role-name BookRecAppRole
```

**Leave alone:** the Bedrock Knowledge Base (`YQUPN7XN9Q`) is shared/course-provided — don't delete unless it's yours to remove.

**Ongoing hygiene:**
- Add an **ECR lifecycle policy** (keep last N images, expire untagged) — each CI deploy pushes a new `:<git-sha>` tag, so images accumulate otherwise.
- CloudWatch retention is already capped at 14 days.
