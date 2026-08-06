"""把「根帳號金鑰」換成一個只能碰東京 EC2 的 IAM 使用者，然後刪掉根金鑰。

**為什麼非做不可**：根帳號金鑰是 AWS 上權限最大的東西，**沒有任何 IAM 政策限制得了它** ——
能改帳單、能刪整個帳號、能碰所有服務。而它是以明文躺在 `~/.aws/credentials` 裡的。
一般 IAM 金鑰外洩，災情可以被政策限制在「一個區域的 EC2」；根金鑰外洩就是整個帳號沒了。

這支只需要跑一次（`python aws_bootstrap_iam.py`），跑完之後：
  - `~/.aws/credentials` 裡是 IAM 使用者的金鑰，權限只有「ap-northeast-1 的 EC2」
  - 根帳號的 access key 已刪除（根帳號本身還在，Console 照樣用 email/密碼登入）

順序刻意是「先驗證新金鑰能用，再刪舊的」—— 反過來的話中間出錯就兩邊都不能用了。
"""
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

USER_NAME = "ticketbot"
POLICY_NAME = "ticketbot-ec2-tokyo"
REGION = "ap-northeast-1"
AWS_DIR = Path.home() / ".aws"

# 用 aws:RequestedRegion 把權限鎖在東京，比套 AmazonEC2FullAccess（全區域）緊得多。
# ssm:GetParameter 是查 Ubuntu AMI id 用的（aws_tokyo.py 不寫死 ami-xxxx）。
POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["ec2:*", "ssm:GetParameter"],
        "Resource": "*",
        "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
    }],
}


def write_credentials(key_id: str, secret: str) -> None:
    AWS_DIR.mkdir(parents=True, exist_ok=True)
    # 不能有 BOM：boto3 會把它當成 section 名稱的一部分，然後說找不到 [default]
    (AWS_DIR / "credentials").write_text(
        f"[default]\naws_access_key_id = {key_id}\naws_secret_access_key = {secret}\n",
        encoding="utf-8")
    (AWS_DIR / "config").write_text(
        f"[default]\nregion = {REGION}\noutput = json\n", encoding="utf-8")


def main():
    iam = boto3.client("iam")
    try:
        who = boto3.client("sts").get_caller_identity()
    except NoCredentialsError:
        raise SystemExit("找不到 AWS 金鑰 —— 先跑 aws_setup_credentials.bat")
    print(f"[WHO] 目前身分: {who['Arn']}")
    if not who["Arn"].endswith(":root"):
        print("[WHO] 已經不是根帳號了，不用換。結束。")
        return

    # --- 1. 建使用者 ---
    try:
        iam.create_user(UserName=USER_NAME)
        print(f"[IAM] 已建立使用者 {USER_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        print(f"[IAM] 使用者 {USER_NAME} 已存在，沿用")

    # --- 2. 掛政策（只能碰東京的 EC2）---
    iam.put_user_policy(UserName=USER_NAME, PolicyName=POLICY_NAME,
                        PolicyDocument=json.dumps(POLICY))
    print(f"[IAM] 政策 {POLICY_NAME} 已套用（僅 {REGION} 的 EC2）")

    # --- 3. 發金鑰 ---
    # 一個使用者最多兩把，重跑時先把舊的清掉免得撞上限
    for k in iam.list_access_keys(UserName=USER_NAME)["AccessKeyMetadata"]:
        iam.delete_access_key(UserName=USER_NAME, AccessKeyId=k["AccessKeyId"])
        print(f"[IAM] 清掉 {USER_NAME} 的舊金鑰 {k['AccessKeyId'][:8]}…")
    new = iam.create_access_key(UserName=USER_NAME)["AccessKey"]
    print(f"[IAM] 新金鑰 {new['AccessKeyId'][:8]}… 已建立")

    # --- 4. 先確認新金鑰真的能用，再動舊的 ---
    print("[CHECK] 驗證新金鑰…", end="", flush=True)
    ok = False
    for _ in range(12):          # 新金鑰要幾秒才會在各區生效
        try:
            boto3.client("ec2", region_name=REGION,
                         aws_access_key_id=new["AccessKeyId"],
                         aws_secret_access_key=new["SecretAccessKey"]
                         ).describe_regions(RegionNames=[REGION])
            ok = True
            break
        except ClientError:
            print(".", end="", flush=True)
            time.sleep(5)
    print(" 通過" if ok else " 失敗")
    if not ok:
        raise SystemExit("新金鑰驗不過，**沒有動根金鑰**，維持原狀。稍後重跑一次即可。")

    # --- 5. 刪根金鑰（一定要在這一步之前完成驗證）---
    root_keys = iam.list_access_keys()["AccessKeyMetadata"]   # 不給 UserName = 呼叫者本人
    for k in root_keys:
        iam.delete_access_key(AccessKeyId=k["AccessKeyId"])
        print(f"[IAM] 已刪除根帳號金鑰 {k['AccessKeyId'][:8]}…")

    # --- 6. 換上新金鑰 ---
    write_credentials(new["AccessKeyId"], new["SecretAccessKey"])
    print(f"[DONE] {AWS_DIR}\\credentials 已換成 {USER_NAME} 的金鑰")

    csv = Path.home() / "Downloads" / "rootkey.csv"
    if csv.exists():
        csv.unlink()
        print(f"[DONE] 已刪除 {csv}（裡面是明文的根金鑰）")

    print("\n根帳號本身沒有動 —— Console 照樣用 email/密碼登入。"
          "\n之後所有操作都走這把只能碰東京 EC2 的金鑰。")


if __name__ == "__main__":
    sys.exit(main())
