"""把 EC2 網卡上的次要私有 IP 掛進作業系統（開機要跑，需 root）。

**AWS 跟 OCI 一樣，只把 IP 配給網卡，guest OS 不會自己掛上**，而 `ip addr add` 又是
暫時的、重開機就沒了。所以要有這支 + 一個 systemd oneshot。

跟 OCI 版（rotate_ips.py --sync-os）的差別：
  - 資料來源是 **IMDSv2**（要先拿 token，EC2 預設已強制），不是雲端 API，
    所以機器上不用放任何金鑰、也不用 IAM 角色
  - AWS 的次要私有 IP **不會自動有公網 IP**，要另外綁 Elastic IP
    （`python aws_tokyo.py multi-ip` 在自己電腦上做）

搶票程式用 `curl_cffi` 的 `interface=<私有IP>` 綁出口，所以這裡掛的私有 IP
就是 GUI 卡片「出口 IP」下拉會看到的那些（webgui 的 /api/local_ips 直接讀網卡）。

用法：
    sudo python3 sync_aws_ips.py          # 補掛缺的
    python3 sync_aws_ips.py --list        # 只看，不需要 root
"""
import json
import subprocess
import sys
import urllib.request

IMDS = "http://169.254.169.254/latest"
TIMEOUT = 5


def imds(path: str) -> str:
    """IMDSv2：先拿 token 再讀。EC2 新機器預設強制 v2，用 v1 會 401。"""
    tok_req = urllib.request.Request(
        f"{IMDS}/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    token = urllib.request.urlopen(tok_req, timeout=TIMEOUT).read().decode()
    req = urllib.request.Request(f"{IMDS}/{path}",
                                 headers={"X-aws-ec2-metadata-token": token})
    return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode()


def wanted() -> tuple[str, str, list[str]]:
    """回 (網卡名, 主 IP, 次要私有 IP 清單)。"""
    mac = imds("meta-data/network/interfaces/macs/").strip().strip("/")
    base = f"meta-data/network/interfaces/macs/{mac}"
    ips = [x for x in imds(f"{base}/local-ipv4s").split("\n") if x.strip()]
    cidr = imds(f"{base}/subnet-ipv4-cidr-block").strip()
    prefix = cidr.split("/")[1]
    # 網卡名：找哪一張的 MAC 對得上
    out = subprocess.run(["ip", "-o", "link"], capture_output=True, text=True).stdout
    nic = "ens5"
    for line in out.splitlines():
        if mac.lower() in line.lower():
            nic = line.split(":")[1].strip().split("@")[0]
            break
    return nic, prefix, ips


def current(nic: str) -> set:
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", nic],
                         capture_output=True, text=True).stdout
    return {l.split()[3].split("/")[0] for l in out.splitlines() if " inet " in l}


def main():
    nic, prefix, ips = wanted()
    have = current(nic)
    primary, secondaries = ips[0], ips[1:]

    print(f"網卡 {nic}   主 IP {primary}   次要 {len(secondaries)} 顆")
    for ip in secondaries:
        print(f"  {ip:<16}{'已掛上' if ip in have else '缺'}")

    if "--list" in sys.argv:
        return 0

    added = 0
    for ip in secondaries:
        if ip in have:
            continue
        r = subprocess.run(["ip", "addr", "add", f"{ip}/{prefix}", "dev", nic],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  掛上 {ip} -> {nic}")
            added += 1
        else:
            err = (r.stderr or "").strip()
            hint = "（要 root 權限）" if "not permitted" in err else ""
            print(f"  {ip} 失敗: {err}{hint}")

    # **也要拿掉 AWS 上已經不存在的** —— 只加不刪的話，在 AWS 移掉一顆次要 IP 之後
    # 網卡上還留著，webgui 的出口 IP 下拉就會列出一個「綁了完全不通」的選項
    # （私有 IP 沒有對應的 Elastic IP 就沒有對外路由），而且是靜默失敗。
    stale = have - set(ips)
    removed = 0
    for ip in sorted(stale):
        r = subprocess.run(["ip", "addr", "del", f"{ip}/{prefix}", "dev", nic],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  移除 {ip}（AWS 上已不存在）")
            removed += 1
        else:
            print(f"  {ip} 移除失敗: {(r.stderr or '').strip()}")

    print(f"完成，補了 {added} 顆、移除 {removed} 顆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
