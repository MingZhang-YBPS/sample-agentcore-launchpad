"""Workshop architecture diagram (static/images/90-organizer-architecture.{zh,en}.webp).

Usage: python build.py <unzipped AWS Architecture Icons package dir (Icon-package_07312026)>
writes arch.zh.html / arch.en.html next to this file; screenshot the #c element at
1600x1500 with device scale 1.5 and save it as WebP (quality 88).
"""
import base64, sys
from pathlib import Path
I=Path(sys.argv[1] if len(sys.argv) > 1 else "icons")
A=I/"Architecture-Service-Icons_07312026"; R=I/"Resource-Icons_07312026"; G=I/"Architecture-Group-Icons_07312026"
ic={
 "cf":A/"Arch_Networking-Content-Delivery/48/Arch_Amazon-CloudFront_48.svg",
 "cfn":A/"Arch_Management-Tools/48/Arch_AWS-CloudFormation_48.svg",
 "ec2":A/"Arch_Compute/48/Arch_Amazon-EC2_48.svg",
 "ac":A/"Arch_Artificial-Intelligence/48/Arch_Amazon-Bedrock-AgentCore_48.svg",
 "br":A/"Arch_Artificial-Intelligence/48/Arch_Amazon-Bedrock_48.svg",
 "cog":A/"Arch_Security-Identity/48/Arch_Amazon-Cognito_48.svg",
 "lam":A/"Arch_Compute/48/Arch_AWS-Lambda_48.svg",
 "apigw":A/"Arch_Networking-Content-Delivery/48/Arch_Amazon-API-Gateway_48.svg",
 "s3":A/"Arch_Storage/48/Arch_Amazon-Simple-Storage-Service_48.svg",
 "cb":A/"Arch_Developer-Tools/48/Arch_AWS-CodeBuild_48.svg",
 "ecr":A/"Arch_Containers/48/Arch_Amazon-Elastic-Container-Registry_48.svg",
 "cw":A/"Arch_Management-Tools/48/Arch_Amazon-CloudWatch_48.svg",
 "xray":A/"Arch_Developer-Tools/48/Arch_AWS-X-Ray_48.svg",
 "efs":A/"Arch_Storage/48/Arch_Amazon-EFS_48.svg",
 "iam":A/"Arch_Security-Identity/48/Arch_AWS-Identity-and-Access-Management_48.svg",
 "ssm":R/"Res_Management-Governance/Res_AWS-Systems-Manager_Run-Command_48.svg",
 "nat":R/"Res_Networking-Content-Delivery/Res_Amazon-VPC_NAT-Gateway_48.svg",
 "igw":R/"Res_Networking-Content-Delivery/Res_Amazon-VPC_Internet-Gateway_48.svg",
 "ep":R/"Res_Networking-Content-Delivery/Res_Amazon-VPC_Endpoints_48.svg",
 "s3f":R/"Res_Storage/Res_Amazon-Simple-Storage-Service_S3-Files_48.svg",
 "users":R/"Res_General-Icons/Res_48_Dark/Res_Users_48_Dark.svg",
 "user":R/"Res_General-Icons/Res_48_Dark/Res_User_48_Dark.svg",
 "g_cloud":G/"AWS-Cloud-logo_32_Dark.svg","g_vpc":G/"Virtual-private-cloud-VPC_32.svg",
 "g_pub":G/"Public-subnet_32.svg","g_priv":G/"Private-subnet_32.svg","g_acct":G/"AWS-Account_32.svg",
}
def uri(k): return "data:image/svg+xml;base64,"+base64.b64encode(ic[k].read_bytes()).decode()
L={
"zh":dict(title="Agent-DLC 工作坊环境架构（每个组一个 AWS 账号）",
 legend_t="由谁创建",legend=["模板：VPC 与私有网络、VPC 端点、S3 Files、EC2、CloudFront、实例角色（指定已有 VPC 时只建安全组、EC2、CloudFront）",
  "分发建好后：Run Command 把 ConsoleUrl 写给后端（PublicBaseUrlAssociation）","实例初始化 make bootstrap：launchpad-base 栈、Registry、Memory、launchpad-gw、Identity 凭证、Transaction Search",
  "参与者在控制台：智能体、知识库与 launchpad-kb-gw、评估器、策略引擎（可选）"],
 part="参与者浏览器",part_s="5 个控制台账号：admin、developer、biz-owner、release-ops、sme",org="组织者",org_s="部署与拆除",
 cloud="AWS 云",acct="AWS 账号 · 区域 us-east-1（默认）",cf_s="HTTPS · *.cloudfront.net\n注入回源密钥头",cfn_s="栈 agent-dlc-launchpad",
 vpc="VPC 10.42.0.0/16（模板新建；也可用已有 VPC 的私有子网）",pub="公有子网（只放 NAT）",priv="两个私有子网（按 AgentCore 可用区 ID 固定）",
 nat="NAT 网关",nat_s="+ 弹性 IP · 出网（apt / pip / npm / GitHub）",igw="互联网网关",
 ec2="EC2 控制台主机",ec2_s="无公网 IP · nginx：没有回源密钥头返回 403\nLaunchpad 后端（FastAPI）+ 控制台前端 · SQLite 台账",
 agents="参与者的 AgentCore 智能体",agents_s="Runtime / Harness 的 ENI\n网络配置档 workshop-private",
 s3f="S3 Files",s3f_s="挂载目标 × 2 + 访问点\n源桶 launchpad-files-*（删栈保留）· 可选 EFS",
 ep="VPC 端点",ep_s="S3 网关端点 + 13 个接口端点：logs · bedrock-runtime · bedrock-agentcore · bedrock-agentcore.gateway · bedrock-agent-runtime · ecr.api · ecr.dkr · xray · secretsmanager · sts · ssm · ssmmessages · ec2messages",
 iam="IAM 实例角色",iam_s="后端以此身份访问 AWS",ssm="Run Command",ssm_s="写入 ConsoleUrl",
 ac="Amazon Bedrock AgentCore",acl=["Runtime","Harness","Gateway","Memory","Identity","Registry","Policy（Cedar）","Evaluations"],
 br="Amazon Bedrock",br_s="模型（Converse）· 知识库",obs="可观测性",cw="CloudWatch Logs",cw_s="Runtime 日志 · aws/spans",xray="X-Ray",xray_s="Transaction Search",
 cdk="CDK 栈 launchpad-base",cog_s="launchpad-users · 网关入站认证",lam_s="示例工具 launchpad-hr-database",apigw_s="示例工具 office-facts",
 s3_s="launchpad-artifacts-* · Agent 包、构建源",cb_s="ARM64 镜像构建",ecr_s="launchpad-agents · 推送即扫描",
 e_https="HTTPS",e_vpco="VPC 源 · HTTP 80",e_create="创建栈",e_mk="创建",e_run="分发建好后",e_dep="部署 / 调用 / 注册",e_ep="AWS API 走私有 DNS",e_mount="NFS 挂载",e_out="出网"),
"en":dict(title="Agent-DLC workshop environment (one AWS account per team)",
 legend_t="Who creates what",legend=["Template: VPC and private network, VPC endpoints, S3 Files, EC2, CloudFront, instance role (with an existing VPC: only the security group, EC2 and CloudFront)",
  "Once the distribution exists: Run Command hands ConsoleUrl to the backend (PublicBaseUrlAssociation)","Instance init make bootstrap: launchpad-base stack, Registry, Memory, launchpad-gw, Identity credentials, Transaction Search",
  "Participants in the console: agents, knowledge bases and launchpad-kb-gw, evaluators, policy engine (optional)"],
 part="Participant browsers",part_s="5 console accounts: admin, developer, biz-owner, release-ops, sme",org="Organiser",org_s="deploys and tears down",
 cloud="AWS Cloud",acct="AWS account · Region us-east-1 (default)",cf_s="HTTPS · *.cloudfront.net\nadds the origin key header",cfn_s="stack agent-dlc-launchpad",
 vpc="VPC 10.42.0.0/16 (created by the template, or a private subnet of an existing VPC)",pub="Public subnet (NAT only)",priv="Two private subnets (pinned to AgentCore AZ IDs)",
 nat="NAT gateway",nat_s="+ Elastic IP · egress (apt / pip / npm / GitHub)",igw="Internet gateway",
 ec2="EC2 console host",ec2_s="no public IP · nginx returns 403 without the origin key\nLaunchpad backend (FastAPI) + console · SQLite ledger",
 agents="Participants' AgentCore agents",agents_s="Runtime / Harness ENIs\nnetwork profile workshop-private",
 s3f="S3 Files",s3f_s="2 mount targets + access point\nsource bucket launchpad-files-* (retained) · optional EFS",
 ep="VPC endpoints",ep_s="S3 gateway endpoint + 13 interface endpoints: logs · bedrock-runtime · bedrock-agentcore · bedrock-agentcore.gateway · bedrock-agent-runtime · ecr.api · ecr.dkr · xray · secretsmanager · sts · ssm · ssmmessages · ec2messages",
 iam="IAM instance role",iam_s="the backend's AWS identity",ssm="Run Command",ssm_s="writes ConsoleUrl",
 ac="Amazon Bedrock AgentCore",acl=["Runtime","Harness","Gateway","Memory","Identity","Registry","Policy (Cedar)","Evaluations"],
 br="Amazon Bedrock",br_s="models (Converse) · knowledge bases",obs="Observability",cw="CloudWatch Logs",cw_s="Runtime logs · aws/spans",xray="X-Ray",xray_s="Transaction Search",
 cdk="CDK stack launchpad-base",cog_s="launchpad-users · gateway inbound auth",lam_s="sample tool launchpad-hr-database",apigw_s="sample tool office-facts",
 s3_s="launchpad-artifacts-* · agent packages, build sources",cb_s="ARM64 image builds",ecr_s="launchpad-agents · scan on push",
 e_https="HTTPS",e_vpco="VPC origin · HTTP 80",e_create="create stack",e_mk="creates",e_run="afterwards",e_dep="deploy / invoke / register",e_ep="AWS APIs over private DNS",e_mount="NFS mount",e_out="egress"),
}
def node(k,x,y,t,s="",w=230):
    s=s.replace("\n","<br>")
    return f'<div class="n" style="left:{x}px;top:{y}px;width:{w}px"><img src="{uri(k)}"><b>{t}</b><span>{s}</span></div>'
def grp(x,y,w,h,label,color,icon=None,dash=True):
    im=f'<img src="{uri(icon)}">' if icon else ""
    return f'<div class="g" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;border-color:{color};border-style:{"dashed" if dash else "solid"}"><div class="gl" style="color:{color}">{im}{label}</div></div>'
def build(lang):
    t=L[lang]; W,H=1600,1500
    h=[f'<div class="title">{t["title"]}</div>']
    h.append(f'<div class="legend"><b>{t["legend_t"]}</b>'+"".join(f"<div>{x}</div>" for x in t["legend"])+'</div>')
    h.append(node("users",60,90,t["part"],t["part_s"],260))
    h.append(node("user",360,90,t["org"],t["org_s"],160))
    h.append(grp(30,250,1540,1220,t["cloud"],"#c9d1d9","g_cloud",False))
    h.append(grp(60,295,1480,1150,t["acct"],"#e7157b","g_acct"))
    h.append(node("cf",90,360,"Amazon CloudFront",t["cf_s"],240))
    h.append(node("cfn",370,340,"AWS CloudFormation",t["cfn_s"],220))
    h.append(node("iam",1310,340,t["iam"],t["iam_s"],200))
    h.append(node("ssm",640,340,t["ssm"],t["ssm_s"],180))
    h.append(grp(90,500,1420,470,t["vpc"],"#8c4fff","g_vpc"))
    h.append(grp(115,560,300,380,t["pub"],"#7aa116","g_pub"))
    h.append(node("nat",145,640,t["nat"],t["nat_s"],240))
    h.append(node("igw",145,800,t["igw"],"",240))
    h.append(grp(440,560,1045,380,t["priv"],"#00a4a6","g_priv"))
    h.append(node("ec2",460,610,t["ec2"],t["ec2_s"],350))
    h.append(node("ac",835,610,t["agents"],t["agents_s"],250))
    h.append(node("s3f",1130,610,t["s3f"],t["s3f_s"],320))
    h.append(f'<div class="n wide" style="left:470px;top:790px;width:990px"><img src="{uri("ep")}"><b>{t["ep"]}</b><span>{t["ep_s"]}</span></div>')
    # AgentCore panel
    h.append(grp(90,1000,860,160,t["ac"],"#01a88d",None,False))
    h.append(f'<img class="pi" src="{uri("ac")}" style="left:110px;top:1045px">')
    h.append('<div class="chips" style="left:190px;top:1050px;width:740px">'+"".join(f"<span>{c}</span>" for c in t["acl"])+'</div>')
    h.append(node("br",975,1010,t["br"],t["br_s"],220))
    h.append(grp(1210,1000,300,200,t["obs"],"#e7157b",None,False))
    h.append(node("cw",1218,1040,t["cw"],t["cw_s"],140))
    h.append(node("xray",1362,1040,t["xray"],t["xray_s"],140))
    h.append(grp(90,1230,1420,190,t["cdk"],"#e7157b"))
    for i,(k,n,s) in enumerate([("cog","Amazon Cognito",t["cog_s"]),("lam","AWS Lambda",t["lam_s"]),("apigw","Amazon API Gateway",t["apigw_s"]),("s3","Amazon S3",t["s3_s"]),("cb","AWS CodeBuild",t["cb_s"]),("ecr","Amazon ECR",t["ecr_s"])]):
        h.append(node(k,110+i*233,1275,n,s,215))
    # edges (x1,y1,x2,y2,label,dashed)
    E=[(190,218,190,355,t["e_https"],0),(300,450,560,612,t["e_vpco"],0,0.22),(440,200,460,335,t["e_create"],0),(480,430,480,500,t["e_mk"],1),
       (590,375,640,375,t["e_run"],1),(730,430,620,605,"",1),(560,740,560,785,t["e_ep"],0),(960,720,960,785,"",0),(1090,665,1130,665,t["e_mount"],1),
       (465,650,392,650,t["e_out"],0,0.5,-8),(258,762,258,795,"",0),(560,860,500,1000,t["e_dep"],0),(1090,860,1090,1005,"",0),(1300,860,1360,1000,"",0),(500,1160,500,1230,"",1)]
    svg=[f'<svg class="e" width="{W}" height="{H}"><defs><marker id="a" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8z" fill="#9aa4b2"/></marker></defs>']
    for e in E:
        x1,y1,x2,y2,lab,d=e[:6]; f=e[6] if len(e)>6 else 0.5; dy=e[7] if len(e)>7 else 0
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#9aa4b2" stroke-width="2" {"stroke-dasharray=\"6 5\"" if d else ""} marker-end="url(#a)"/>')
        if lab: svg.append(f'<text x="{x1+(x2-x1)*f+8}" y="{y1+(y2-y1)*f+dy}" class="el">{lab}</text>')
    svg.append("</svg>")
    css=f'''body{{margin:0;background:#0d1117;font-family:"Noto Sans CJK SC","PingFang SC",system-ui,sans-serif;color:#e6edf3}}
#c{{position:relative;width:{W}px;height:{H}px}}
.title{{position:absolute;left:0;right:0;top:18px;text-align:center;font-size:26px;font-weight:700}}
.legend{{position:absolute;left:600px;top:70px;width:950px;background:#3b2f0f;border:1px solid #d29922;border-radius:10px;padding:10px 14px;font-size:14px;line-height:1.55;color:#f2e2b8;z-index:3}}
.legend b{{color:#f0b429}}
.n{{position:absolute;display:flex;flex-direction:column;align-items:center;text-align:center;z-index:2}}
.n img{{width:56px;height:56px}} .n b{{font-size:16px;margin-top:6px}} .n span{{font-size:13px;color:#9aa4b2;line-height:1.45;margin-top:2px}}
.n.wide{{flex-direction:row;text-align:left;gap:14px;background:#11161d;border:1px solid #30363d;border-radius:8px;padding:10px 14px;box-sizing:border-box}}
.n.wide b{{white-space:nowrap;margin:0}} .n.wide span{{margin:0}}
.g{{position:absolute;border:2px solid;border-radius:6px;box-sizing:border-box;z-index:1}}
.gl{{position:absolute;left:8px;top:-1px;font-size:15px;font-weight:600;display:flex;align-items:center;gap:6px;padding:4px 6px}}
.gl img{{width:28px;height:28px}}
.pi{{position:absolute;width:64px;height:64px;z-index:2}}
.chips{{position:absolute;display:flex;flex-wrap:wrap;gap:10px;z-index:2}}
.chips span{{background:#0f2a26;border:1px solid #01a88d;color:#c8f3ea;border-radius:999px;padding:6px 14px;font-size:15px}}
svg.e{{position:absolute;left:0;top:0;z-index:2;pointer-events:none}} .el{{fill:#c9d1d9;font-size:13px;paint-order:stroke;stroke:#0d1117;stroke-width:4px}}'''
    return f'<html><head><meta charset="utf-8"><style>{css}</style></head><body><div id="c">{"".join(h)}{"".join(svg)}</div></body></html>'
for lang in ["zh","en"]:
    Path(__file__).with_name(f"arch.{lang}.html").write_text(build(lang))
print("ok")
