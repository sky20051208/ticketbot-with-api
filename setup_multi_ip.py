"""在 OCI 機器的 VNIC 上備妥「N 顆次要私有 IP，每顆各配一顆 reserved public IP」。

為什麼要這個：多開時每個 instance 需要自己的出口 IP，不然拓元看到的是同一顆 IP
開了十幾個 session。住宅代理雖然也能換 IP，但實測從 Ashburn 走 CliProxy 打拓元要
281ms、直連只要 69ms（慢 4 倍，而且慢的是「繞去美國家庭寬頻再回來」那兩段消費級
線路，無法優化）。同一台機器掛多顆公網 IP 就沒有這個代價。

搶票程式用 `curl_cffi` 的 `interface=<私有IP>` 綁出口（見 tixcraftapi/session.py），
Chrome 那邊走 browser_login.setup_bind_proxy —— 兩邊都綁同一顆，因為 eps_sid
綁發放時的出口 IP。config 填的是**私有** IP（LOCAL_BIND_IP），公網 IP 是 OCI 自己對應的。

跟 rotate_ips.py 的分工：
    setup_multi_ip.py  建置（一次性）—— 跑在**你自己電腦**上，用你的 OCI CLI 憑證
    rotate_ips.py      維運（換 IP / 開機同步）—— 跑在**機器**上，走 Instance Principals

用法：
    python setup_multi_ip.py --list                 # 只看現況
    python setup_multi_ip.py --count 6              # 備妥 6 顆
    python setup_multi_ip.py --count 6 --dry-run    # 演練
    python setup_multi_ip.py --release-from <instance-ocid>   # 先從舊機器收回配額

**配額很緊**：reserved public IP 預設只有 6 顆（`oci limits value list --service-name
vcn --name reserved-public-ip-count`）。所以會**優先重用已經是 AVAILABLE 的**，不夠才新建。
"""
import argparse
import json
import subprocess
import sys
import time

SETTLE = 8          # 解除綁定後等 OCI 真的收回配額；rotate_ips.py 實測 3 秒不夠


def oci_cli(*args, ok_empty=False):
    """跑 oci CLI 回 data 部分。失敗直接 raise —— 建置腳本不該吞錯。"""
    cmd = ["oci", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        if ok_empty and "NotAuthorizedOrNotFound" in (r.stderr or ""):
            return None
        raise RuntimeError(f"oci {' '.join(args[:3])} 失敗:\n{r.stderr[:500]}")
    if not (r.stdout or "").strip():
        return None
    return json.loads(r.stdout).get("data")


def instance_vnic(instance_id):
    v = oci_cli("compute", "instance", "list-vnics", "--instance-id", instance_id)
    if not v:
        raise RuntimeError("這台機器沒有 VNIC（開機了嗎？）")
    return v[0]["id"], v[0]["compartment-id"]


def private_ips(vnic_id):
    return oci_cli("network", "private-ip", "list", "--vnic-id", vnic_id) or []


def public_ips(compartment_id):
    return oci_cli("network", "public-ip", "list", "--compartment-id", compartment_id,
                   "--scope", "REGION", "--all") or []


def show(vnic_id, compartment_id, title="現況"):
    privs = private_ips(vnic_id)
    pubs = {p.get("private-ip-id"): p for p in public_ips(compartment_id)
            if p.get("private-ip-id")}
    print(f"\n=== {title} ===")
    print(f"{'私有 IP':<14}{'primary':<10}{'公網 IP':<18}種類")
    for p in sorted(privs, key=lambda x: not x["is-primary"]):
        pub = pubs.get(p["id"])
        kind = "reserved" if pub else ("ephemeral（主 IP 用，不佔配額）"
                                       if p["is-primary"] else "（無）")
        print(f"{p['ip-address']:<14}{str(p['is-primary']):<10}"
              f"{(pub['ip-address'] if pub else '-'):<18}{kind}")
    free = [p for p in public_ips(compartment_id)
            if p["lifecycle-state"] == "AVAILABLE"]
    print(f"\n  未綁定的 reserved public IP: {[p['ip-address'] for p in free] or '無'}")
    return privs, free


def release_from(instance_id, dry_run):
    """把另一台機器上的 reserved public IP 解除綁定（**不刪它的私有 IP**，可逆）。"""
    vnic_id, comp = instance_vnic(instance_id)
    ids = {p["id"]: p["ip-address"] for p in private_ips(vnic_id)}
    freed = 0
    for pub in public_ips(comp):
        if pub.get("private-ip-id") in ids:
            print(f"  解除 {pub['ip-address']} ← {ids[pub['private-ip-id']]}")
            if not dry_run:
                oci_cli("network", "public-ip", "update", "--public-ip-id", pub["id"],
                        "--private-ip-id", "", "--force")
                freed += 1
    if freed:
        print(f"  等 {SETTLE}s 讓 OCI 收回配額…")
        time.sleep(SETTLE)
    print(f"  釋出 {freed} 顆")


def provision(vnic_id, compartment_id, count, dry_run):
    privs = private_ips(vnic_id)
    secondaries = [p for p in privs if not p["is-primary"]]
    print(f"\n目前次要私有 IP {len(secondaries)} 顆，目標 {count} 顆")

    # 1. 補足次要私有 IP
    for i in range(len(secondaries), count):
        print(f"  建立第 {i + 1} 顆次要私有 IP…", end=" ", flush=True)
        if dry_run:
            print("(dry-run)")
            continue
        p = oci_cli("network", "private-ip", "create", "--vnic-id", vnic_id)
        print(p["ip-address"])
        secondaries.append(p)

    if dry_run:
        return

    # 2. 每顆配一顆 reserved public IP；先用 AVAILABLE 的，不夠才新建
    pubs = public_ips(compartment_id)
    assigned = {p.get("private-ip-id") for p in pubs if p.get("private-ip-id")}
    spare = [p for p in pubs if p["lifecycle-state"] == "AVAILABLE"]

    for p in secondaries[:count]:
        if p["id"] in assigned:
            continue
        if spare:
            pub = spare.pop(0)
            print(f"  {p['ip-address']} ← 重用 {pub['ip-address']}")
            oci_cli("network", "public-ip", "update", "--public-ip-id", pub["id"],
                    "--private-ip-id", p["id"], "--force")
        else:
            print(f"  {p['ip-address']} ← 新建 reserved public IP…", end=" ", flush=True)
            try:
                new = oci_cli("network", "public-ip", "create",
                              "--compartment-id", compartment_id,
                              "--lifetime", "RESERVED", "--private-ip-id", p["id"])
                print(new["ip-address"])
            except RuntimeError as e:
                print("失敗")
                if "LimitExceeded" in str(e) or "Limit" in str(e):
                    print("     → reserved public IP 配額用完了。"
                          "先跑 --release-from <舊機器 OCID> 收回，或去 OCI Console 提高配額")
                    return
                raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance-id", help="目標機器 OCID（省略時讀 vps.config.ps1）")
    ap.add_argument("--count", type=int, default=6, help="要幾顆次要私有 IP（預設 6）")
    ap.add_argument("--list", action="store_true", help="只看現況不動任何東西")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--release-from", help="先從這台機器解除 reserved public IP 綁定")
    args = ap.parse_args()

    instance_id = args.instance_id
    if not instance_id:
        import re
        import pathlib
        txt = pathlib.Path(__file__).with_name("vps.config.ps1").read_text(encoding="utf-8")
        m = re.search(r'^\$VpsInstanceOcid = "(.*)"', txt, re.M)
        if not m:
            sys.exit("vps.config.ps1 裡找不到 $VpsInstanceOcid，請用 --instance-id 指定")
        instance_id = m.group(1)

    vnic_id, comp = instance_vnic(instance_id)
    show(vnic_id, comp)
    if args.list:
        return

    if args.release_from:
        print("\n=== 從舊機器收回配額 ===")
        release_from(args.release_from, args.dry_run)

    provision(vnic_id, comp, args.count, args.dry_run)
    show(vnic_id, comp, "完成後")
    print("\n下一步：到機器上跑 `python3 rotate_ips.py --sync-os` 把這些 IP 掛進作業系統")


if __name__ == "__main__":
    main()
