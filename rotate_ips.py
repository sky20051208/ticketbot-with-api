"""輪替這台 OCI 機器的公網 IP —— 用過的 IP 換掉，私有 IP 不動。

**為什麼私有 IP 不動很重要**：搶票程式是用 `curl_cffi` 的 `interface=<私有IP>` 綁定出口
（見 tixcraftapi/session.py），私有 IP 保持不變 = config 不用改、netplan 不用改，
只是封包從新的公網 IP 出去。

機制：OCI 的次要私有 IP 只能配 **reserved** public IP（不能用 ephemeral），所以「換 IP」
= 刪掉舊的 reserved public IP + 建一顆新的綁回同一個私有 IP。Oracle 從該區域的池子隨機
給一顆，**有低機率拿到剛剛那顆**，介意的話用 --retry 多換幾次。

認證走 **Instance Principals**，機器上不放任何金鑰。前置（OCI Console 各做一次）：
  1. Dynamic group `tixcraft-vms`，規則 `instance.compartment.id = '<compartment OCID>'`
  2. Policy：Allow dynamic-group tixcraft-vms to manage public-ips in tenancy
             Allow dynamic-group tixcraft-vms to use private-ips in tenancy
             Allow dynamic-group tixcraft-vms to read instance-family in tenancy
             Allow dynamic-group tixcraft-vms to use virtual-network-family in tenancy

用法：
    pip install oci
    python3 rotate_ips.py --list              # 只看現況
    python3 rotate_ips.py --all               # 全部次要 IP 換一輪
    python3 rotate_ips.py --ip 10.0.0.88      # 只換這顆
    python3 rotate_ips.py --all --dry-run     # 演練，不真的動

**主要私有 IP（primary）不會被碰** —— 它配的是 ephemeral public IP，換掉會斷你的 SSH。
"""
import argparse
import subprocess
import sys
import time
import urllib.request

import oci

METADATA = "http://169.254.169.254/opc/v2/instance/"
# OCI 刪掉的 reserved public IP **不會立刻釋放配額**，配額卡很緊時（預設可能只有 4 顆）
# 一定要等它真的收回，不然 create 會撞 LimitExceeded。實測 3 秒不夠。
SETTLE = 10
CREATE_TRIES = 6
CREATE_BACKOFF = 15


def instance_metadata() -> dict:
    req = urllib.request.Request(METADATA, headers={"Authorization": "Bearer Oracle"})
    import json
    return json.load(urllib.request.urlopen(req, timeout=10))


def build_clients():
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return (oci.core.ComputeClient(config={}, signer=signer),
            oci.core.VirtualNetworkClient(config={}, signer=signer),
            signer)


def collect(compute, network, instance_id, compartment_id):
    """回 [(private_ip 物件, public_ip 物件 or None)]，primary 排最前面。"""
    atts = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
    rows = []
    for att in atts:
        if att.lifecycle_state != "ATTACHED":
            continue
        for pip in network.list_private_ips(vnic_id=att.vnic_id).data:
            pub = None
            try:
                pub = network.get_public_ip_by_private_ip_id(
                    oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=pip.id)
                ).data
            except oci.exceptions.ServiceError as e:
                if e.status != 404:
                    raise
            rows.append((pip, pub))
    rows.sort(key=lambda r: (not r[0].is_primary, r[0].ip_address))
    return rows


def os_addresses(iface: str) -> set:
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface],
                         capture_output=True, text=True).stdout
    return {line.split()[3].split("/")[0] for line in out.splitlines() if len(line.split()) > 3}


def sync_os(rows) -> int:
    """把 OCI 上有、但 OS 網卡沒掛的次要私有 IP 補上去。

    `ip addr add` 重開機就沒了，而 OCI 只負責把 IP 配給 VNIC、不會動到 guest OS 的設定。
    所以每次開機要跑一次（搭配 systemd unit）。直接從 OCI 讀，Console 上新增 IP 之後
    不用改任何設定檔。
    """
    primary = next(p.ip_address for p, _ in rows if p.is_primary)
    iface = subprocess.run(
        ["bash", "-c", f"ip -br addr | awk '/{primary}/{{print $1}}'"],
        capture_output=True, text=True).stdout.strip()
    if not iface:
        print(f"找不到掛著 {primary} 的網卡")
        return 1

    have = os_addresses(iface)
    added = 0
    for pip, _ in rows:
        if pip.ip_address in have:
            continue
        r = subprocess.run(["ip", "addr", "add", f"{pip.ip_address}/24", "dev", iface],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  掛上 {pip.ip_address} -> {iface}")
            added += 1
        else:
            print(f"  {pip.ip_address} 失敗: {r.stderr.strip()}（要 root 權限）")
    print(f"完成，補了 {added} 顆（網卡 {iface}）")
    return 0


def rotate_one(network, compartment_id, pip, pub, dry_run: bool) -> str:
    old = pub.ip_address if pub else "(無)"
    if dry_run:
        print(f"  {pip.ip_address:<14} {old:<16} -> (dry-run，未動)")
        return old

    if pub:
        network.delete_public_ip(pub.id)
        time.sleep(SETTLE)

    details = oci.core.models.CreatePublicIpDetails(
        compartment_id=compartment_id,
        lifetime="RESERVED",
        private_ip_id=pip.id,
        display_name=f"rot-{pip.ip_address.replace('.', '-')}",
    )
    for attempt in range(1, CREATE_TRIES + 1):
        try:
            new = network.create_public_ip(details).data
            print(f"  {pip.ip_address:<14} {old:<16} -> {new.ip_address}")
            return new.ip_address
        except oci.exceptions.ServiceError as e:
            if e.code != "LimitExceeded" or attempt == CREATE_TRIES:
                # 這顆現在是「舊的已刪、新的沒建成」的半殘狀態，講清楚讓人知道要補
                print(f"  {pip.ip_address:<14} {old:<16} -> 失敗（{e.code}）"
                      f"  ← 這顆目前沒有公網 IP，配額夠了之後跑 --ip {pip.ip_address} 補建")
                raise
            print(f"    配額還沒釋放，{CREATE_BACKOFF}s 後重試（{attempt}/{CREATE_TRIES}）")
            time.sleep(CREATE_BACKOFF)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="只列出現況，不動任何東西")
    g.add_argument("--all", action="store_true", help="輪替所有次要 IP")
    g.add_argument("--ip", help="只輪替這個私有 IP，例 10.0.0.88")
    g.add_argument("--sync-os", action="store_true",
                   help="把 OCI 上的次要私有 IP 補掛到網卡（開機要跑，需 root）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    meta = instance_metadata()
    instance_id, compartment_id = meta["id"], meta["compartmentId"]
    compute, network, _ = build_clients()
    rows = collect(compute, network, instance_id, compartment_id)

    print("=" * 62)
    print(f"  {'私有 IP':<14} {'公網 IP':<16} 類型")
    for pip, pub in rows:
        kind = "primary（不動）" if pip.is_primary else (pub.lifetime if pub else "無公網 IP")
        print(f"  {pip.ip_address:<14} {(pub.ip_address if pub else '-'):<16} {kind}")
    print("=" * 62)

    if args.list:
        return
    if args.sync_os:
        return sync_os(rows)

    targets = [(p, q) for p, q in rows if not p.is_primary
               and (args.all or p.ip_address == args.ip)]
    if not targets:
        print("沒有符合的次要 IP（primary 一律跳過，換掉會斷 SSH）")
        return 1

    print(f"\n輪替 {len(targets)} 顆{'（dry-run）' if args.dry_run else ''}：")
    for pip, pub in targets:
        rotate_one(network, compartment_id, pip, pub, args.dry_run)
    print("\n完成。私有 IP 沒變，程式裡的 interface= 不用改。")


if __name__ == "__main__":
    sys.exit(main())
