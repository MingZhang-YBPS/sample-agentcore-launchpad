# Private networking: inbound PrivateLink, a private console, VPC teardown

中文版: [private-networking.zh-CN.md](private-networking.zh-CN.md)

Agents run in a customer VPC through **network profiles** (Configure → Network
profiles, `/v3/network`; design projects §12). This page covers what happens around
them: how private callers reach an agent, how the Launchpad console itself can be
deployed without a public IP, what the platform tells you about telemetry and cost,
and the order to tear a VPC down in.

## 1. Private inbound to agents (AgentCore PrivateLink)

Internal applications invoke agents through the interface VPC endpoint
`com.amazonaws.<region>.bedrock-agentcore` (data plane); the control plane has its own,
`com.amazonaws.<region>.bedrock-agentcore-control`.

The profile preflight (`POST /api/network-profiles/{id}/check`) reports this as the
informational entry `inbound.endpoint` — it never blocks a profile:

- whether the data-plane / control-plane endpoints exist in the profile's VPC, their
  private-DNS setting and subnets;
- the principals the endpoint policy's `Allow` statements name, and whether it admits
  everyone (`*`, also the default full-access policy).

**An endpoint policy can only restrict IAM principals.** A caller authenticating with
OAuth (an agent with inbound JWT authorization) appears to the endpoint policy as
`Principal: "*"`. For such agents the deploy-time preflight adds that note, and the
agent's inbound JWT authorizer (`routers/identity.py` inbound-auth settings) is what
actually limits callers.

## 2. A private Launchpad console (`ConsoleExposure`)

The workshop template (`static/infrastructure/launchpad-multiuser.yaml`, kept in the
workshop repository, not here) requires a public subnet today: CloudFront's origin is
the instance's `PublicDnsName` and the instance gets a public IPv4. The template needs
a new parameter:

```yaml
ConsoleExposure:
  Type: String
  AllowedValues: [public-instance, cloudfront-vpc-origin, internal-alb]
  Default: public-instance
CreateEndpoints:          # internal-alb only: create the interface endpoints below
  Type: String
  AllowedValues: ["true", "false"]
  Default: "false"
```

| Value | What the template must do | Notes |
|---|---|---|
| `public-instance` (default, today) | Public subnet, public IPv4, CloudFront origin = `PublicDnsName`, origin secret header | Unchanged; what the workshop has verified |
| `cloudfront-vpc-origin` | Instance in a private subnet (`ExistingSubnetId` no longer has to be public, `AssociatePublicIpAddress: false`); a CloudFront **VPC origin** (`AWS::CloudFront::VpcOrigin`) pointing at the instance or an internal ALB; the instance security group admits only the CloudFront VPC-origin managed prefix list / security group on the console port | Keeps the CloudFront HTTPS name without a public IP. Confirm VPC origins are available in the target region and which origin types it supports (EC2 / ALB / NLB). The instance still needs outbound access: NAT, or every endpoint listed below, or a pre-baked AMI (bootstrap installs packages from the internet) |
| `internal-alb` | Internal ALB in private subnets, an ACM certificate, a Route 53 private hosted zone record; no CloudFront | Fully private: reachable over VPN / Direct Connect only. Workshop participants cannot reach it, and `/v1` serves internal callers only |

Recommendation: the template default stays `public-instance`; enterprise deployment
docs recommend `internal-alb`; `cloudfront-vpc-origin` is the middle ground for "a
public HTTPS name but no public IP".

With the console host in a private subnet, every AWS API the hub calls must be reached
through NAT or an interface endpoint: `sts`, `iam`, `s3` (gateway), `ecr.api`,
`ecr.dkr`, `logs`, `codebuild`, `cognito-idp`, `secretsmanager`, `bedrock`,
`bedrock-runtime`, `bedrock-agentcore`, `bedrock-agentcore-control`, `xray`,
`monitoring` (CloudWatch), `servicequotas`, `ec2`. `CreateEndpoints=true` creates them
(with private DNS, in the console subnets, a security group admitting 443 from the
instance). `iam` is a global service with no regional endpoint in most regions — keep a
NAT path for it or confirm the endpoint exists in the target region.

Template tests: `cfn-lint`, and a deploy smoke test per `ConsoleExposure` value
(`cloudfront-vpc-origin` in a region that supports VPC origins).

## 3. Telemetry and cost of VPC agents

- **Telemetry.** A VPC agent without a reachable `logs` (or `xray`) endpoint loses its
  logs and spans silently. Preflight blocks on `logs` (every agent needs it) and warns
  on `xray`. The agent's Network card shows "this profile cannot reach logs / xray"
  when the profile's last preflight failed those rows; the backend helper
  `services/network_profiles.telemetry_hint(db, workspace_id, spec)` gives the same
  hint for the observability page to show when an agent had invocations but no spans
  in the last 30 minutes.
- **Cost.** The preflight's informational `cost` entry counts the interface endpoints
  in the VPC × the profile's Availability Zones and the NAT gateways its subnets route
  through; the profile page links the PrivateLink and VPC (NAT) price lists. The
  platform does not price them (prices vary by region) and does not create endpoints or
  NAT gateways — they are the customer's network resources.

## 4. Teardown: delete the agents before the VPC

AgentCore's network service-linked role (`AWSServiceRoleForBedrockAgentCoreNetwork`)
holds ENIs in the profile's subnets and releases them some time after the runtime /
harness is deleted; until then the subnets and security groups cannot be deleted.

1. Delete every agent that uses the profile (the profile page's "Used by" column; a
   profile cannot be archived while a live agent names it — `network_profile.in_use`).
2. Each delete of a VPC agent starts an `agent_eni_release` job that polls
   `ec2:DescribeNetworkInterfaces` (filtered by the profile's security groups and
   subnets) for a few minutes, then hands over to the scheduled teardown, which keeps
   checking for up to 6 hours and then fails the job with the ENI ids still present.
   While such a job runs, the profile cannot be archived (`network_profile.enis_pending`).
3. When the jobs have succeeded, archive the profile, then delete the subnets, security
   groups and endpoints in the VPC.

ENIs carry no documented tag or description format naming the agent: when several
agents share a profile, only ENIs whose description names the deleted runtime are
waited for.
