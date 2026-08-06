"""建立 / 管理遠大 TicketPlus 專用的東京 EC2（ap-northeast-1）。

**為什麼要東京**：TicketPlus 的 origin 就在 AWS 東京（實測：東京探針到 `queue` 只有 3ms，
台北 36ms，美東 165ms）。送單的 `queue` / `api` 兩支**沒掛 CDN**，純看距離。
拓元那台在美東是因為 tixcraft 的 origin 在美東 —— 兩邊剛好相反，不能共用一台。

**規格為什麼是這樣**：
  - 記憶體 16GB：實測 PSS 每組 Chrome 210MB + Python 80MB，30 組 ≈ 8.7GB，加 OS 和餘裕
  - 4 vCPU：Sunshine 的 H.264 軟編實測吃掉 0.85 核，留 3 核給 Chrome 和搶票
  - **刻意不用 t 系列**：t3/t4g 是 burstable，CPU 額度用完會被降速。一次要跑 3 小時，
    中途被 throttle 會很難看
  - 遠大沒有驗證碼、不跑 OCR，所以 CPU 需求比拓元那台輕很多

用法：
    python aws_tokyo.py create      建立（已存在就只印出來，不會重複開）
    python aws_tokyo.py start       開機
    python aws_tokyo.py stop        關機
    python aws_tokyo.py status      看狀態與公網 IP
    python aws_tokyo.py sync-ip     把安全群組的來源限制同步成你當下的對外 IP
    python aws_tokyo.py terminate   砍掉（要打字確認）

前置：先跑一次 aws_setup_credentials.bat 設定金鑰。
"""
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

REGION = "ap-northeast-1"           # 東京
NAME = "ticketplus-tokyo"
INSTANCE_TYPE = "m6i.xlarge"        # 4 vCPU / 16GB，非 burstable
DISK_GB = 50                        # 30 組 Chrome profile 各約 64MB，再加 swap 和 repo
# Canonical 官方維護的參數，永遠指向最新的 Ubuntu 24.04 AMI —— 比寫死 ami-xxxx 好，
# 那個每次改版都會失效，而且每個 region 的 id 都不一樣
UBUNTU_SSM = ("/aws/service/canonical/ubuntu/server/24.04/stable/current"
              "/amd64/hvm/ebs-gp3/ami-id")
KEY_PATH = Path.home() / ".ssh" / f"{NAME}.pem"

# Sunshine 串流埠。**48010/TCP 是 RTSP 交握**，漏掉的話配對得成、串流卻會逾時（拓元那台踩過）。
# 47990 是網頁管理 UI，刻意不對外開 —— 走 SSH 通道就好。
STREAM_RULES = [("tcp", 47984, 47989), ("tcp", 48010, 48010), ("udp", 47998, 48010)]


def ec2():
    return boto3.client("ec2", region_name=REGION)


def my_ip() -> str:
    import urllib.request
    with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
        return r.read().decode().strip()


def find_instance() -> dict | None:
    """回這台機器的描述；沒有或已終止回 None。"""
    res = ec2().describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NAME]},
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ])
    for r in res["Reservations"]:
        for i in r["Instances"]:
            return i
    return None


def ensure_key_pair() -> str:
    """確保有金鑰對，私鑰存在 ~/.ssh/。AWS 只在建立當下給一次私鑰，弄丟就只能重建。"""
    c = ec2()
    try:
        c.describe_key_pairs(KeyNames=[NAME])
        if not KEY_PATH.exists():
            raise SystemExit(
                f"AWS 上有金鑰對 {NAME} 但本機找不到 {KEY_PATH}。\n"
                f"私鑰只在建立當下給一次，救不回來 —— 請先到 Console 刪掉那個 key pair "
                f"（EC2 → Key Pairs），再重跑一次 create。")
        return NAME
    except ClientError as e:
        if "NotFound" not in str(e):
            raise
    print(f"[KEY] 建立金鑰對 {NAME}")
    kp = c.create_key_pair(KeyName=NAME, KeyType="ed25519")
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(kp["KeyMaterial"])
    # Windows 的 OpenSSH 會檢查權限，太寬鬆會拒絕使用；這裡只留當前使用者
    if sys.platform == "win32":
        import subprocess
        subprocess.run(["icacls", str(KEY_PATH), "/inheritance:r",
                        "/grant:r", f"{Path.home().name}:R"],
                       capture_output=True)
    else:
        KEY_PATH.chmod(0o600)
    print(f"[KEY] 私鑰已存到 {KEY_PATH}")
    return NAME


def default_vpc() -> str:
    res = ec2().describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not res["Vpcs"]:
        raise SystemExit(f"{REGION} 沒有 default VPC。到 VPC Console 按 Actions → "
                         f"Create default VPC 建一個再重跑。")
    return res["Vpcs"][0]["VpcId"]


def ensure_security_group(vpc_id: str) -> str:
    c = ec2()
    res = c.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [NAME]},
        {"Name": "vpc-id", "Values": [vpc_id]}])
    if res["SecurityGroups"]:
        return res["SecurityGroups"][0]["GroupId"]
    print(f"[SG] 建立安全群組 {NAME}")
    sg = c.create_security_group(GroupName=NAME, VpcId=vpc_id,
                                 Description="TicketPlus Tokyo bot")
    return sg["GroupId"]


def sync_ip(quiet: bool = False) -> None:
    """把 SSH 和串流埠的來源限制同步成「你家當下的對外 IP」。

    為什麼不直接對全網開：那台機器上有 20~30 個客人的購票帳號登入態。而家用 IP 本來
    就會變（重撥、搬家），所以每次開機都自動同步 —— 比手動維護白名單可靠。
    """
    inst = find_instance()
    if not inst:
        raise SystemExit("還沒建立機器，先跑 create")
    sg_id = inst["SecurityGroups"][0]["GroupId"]
    cidr = f"{my_ip()}/32"
    c = ec2()
    current = c.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]

    wanted = [("tcp", 22, 22)] + STREAM_RULES
    have = {(p["IpProtocol"], p.get("FromPort"), p.get("ToPort"), r["CidrIp"])
            for p in current["IpPermissions"] for r in p.get("IpRanges", [])}
    if all((proto, lo, hi, cidr) in have for proto, lo, hi in wanted):
        if not quiet:
            print(f"[SG] 規則已經是 {cidr}，不用改")
        return

    # 先撤掉舊的（IP 換了就是撤掉舊 IP 的那幾條），再加新的
    if current["IpPermissions"]:
        c.revoke_security_group_ingress(GroupId=sg_id,
                                        IpPermissions=current["IpPermissions"])
    c.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[
        {"IpProtocol": proto, "FromPort": lo, "ToPort": hi,
         "IpRanges": [{"CidrIp": cidr, "Description": NAME}]}
        for proto, lo, hi in wanted])
    print(f"[SG] 來源已同步為 {cidr}（SSH + Sunshine 串流埠）")


def create() -> None:
    existing = find_instance()
    if existing:
        print(f"[CREATE] 已經有一台了（{existing['InstanceId']}，"
              f"{existing['State']['Name']}），不重複建立")
        return status()
    c = ec2()
    ami = boto3.client("ssm", region_name=REGION).get_parameter(
        Name=UBUNTU_SSM)["Parameter"]["Value"]
    print(f"[CREATE] Ubuntu 24.04 AMI: {ami}")
    vpc_id = default_vpc()
    ensure_key_pair()
    sg_id = ensure_security_group(vpc_id)

    res = c.run_instances(
        ImageId=ami, InstanceType=INSTANCE_TYPE, MinCount=1, MaxCount=1,
        KeyName=NAME, SecurityGroupIds=[sg_id],
        BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
            "VolumeSize": DISK_GB, "VolumeType": "gp3", "DeleteOnTermination": True}}],
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": [{"Key": "Name", "Value": NAME}]}],
    )
    iid = res["Instances"][0]["InstanceId"]
    print(f"[CREATE] {iid} 建立中（{INSTANCE_TYPE}, {DISK_GB}GB）…")
    c.get_waiter("instance_running").wait(InstanceIds=[iid])
    sync_ip(quiet=True)
    status()


def _wait_state(iid: str, waiter: str, label: str) -> None:
    print(f"[{label}] 等待中…", end="", flush=True)
    ec2().get_waiter(waiter).wait(InstanceIds=[iid])
    print(" 完成")


def start() -> None:
    inst = find_instance()
    if not inst:
        raise SystemExit("還沒建立機器，先跑 create")
    if inst["State"]["Name"] != "running":
        ec2().start_instances(InstanceIds=[inst["InstanceId"]])
        _wait_state(inst["InstanceId"], "instance_running", "START")
    sync_ip(quiet=True)      # 公網 IP 每次開機都會換，家裡 IP 也可能換
    status()


def stop() -> None:
    inst = find_instance()
    if not inst:
        raise SystemExit("還沒建立機器")
    if inst["State"]["Name"] == "stopped":
        print("[STOP] 本來就是關機狀態")
        return
    ec2().stop_instances(InstanceIds=[inst["InstanceId"]])
    _wait_state(inst["InstanceId"], "instance_stopped", "STOP")


def status() -> None:
    inst = find_instance()
    if not inst:
        print("還沒建立機器（跑 create）")
        return
    ip = inst.get("PublicIpAddress", "")
    print("=" * 62)
    print(f"  {NAME}  {inst['InstanceId']}  {inst['InstanceType']}")
    print(f"  狀態    : {inst['State']['Name']}")
    print(f"  公網 IP : {ip or '（關機中沒有）'}")
    if ip:
        print(f"  SSH     : ssh -i {KEY_PATH} ubuntu@{ip}")
    print("=" * 62)


def terminate() -> None:
    inst = find_instance()
    if not inst:
        raise SystemExit("沒有機器可以砍")
    print(f"要砍掉 {inst['InstanceId']}（{INSTANCE_TYPE}）—— 磁碟一起刪，救不回來。")
    if input("確定的話輸入 terminate：").strip() != "terminate":
        print("取消")
        return
    ec2().terminate_instances(InstanceIds=[inst["InstanceId"]])
    print("[TERMINATE] 已送出")


def main():
    actions = {"create": create, "start": start, "stop": stop, "status": status,
               "sync-ip": sync_ip, "terminate": terminate}
    if len(sys.argv) < 2 or sys.argv[1] not in actions:
        raise SystemExit(f"用法: python aws_tokyo.py [{' | '.join(actions)}]")
    try:
        actions[sys.argv[1]]()
    except NoCredentialsError:
        raise SystemExit("找不到 AWS 金鑰 —— 先跑一次 aws_setup_credentials.bat")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AuthFailure", "UnauthorizedOperation", "InvalidClientTokenId"):
            raise SystemExit(f"AWS 金鑰有問題（{code}）—— 先跑 aws_setup_credentials.bat")
        raise


if __name__ == "__main__":
    sys.exit(main())
