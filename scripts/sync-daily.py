# sync-daily.py
# Endfield Pool Assets 每日同步脚本
# 1. 同步远端 TableCfg
# 2. 遍历 pool_id，新池走官方 API，旧池回填历史数据
# 3. 下载横幅/轮换图
# 4. 合并到 GachaPoolTable.json
# 5. 检测缺失肖像

import json, os, sys, hashlib, shutil, tempfile
from pathlib import Path
from datetime import datetime, timezone

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DATA_DIR = PUBLIC_DIR / "data"
PUBLIC_IMG_DIR = PUBLIC_DIR / "images"
RAW_TABLECFG_DIR = BASE_DIR / "raw" / "TableCfg"
HISTORICAL_TABLE_PATH = PUBLIC_DATA_DIR / "GachaPoolTable.json"

TABLECFG_REMOTE_BASE = "https://lulush.microgg.cn/BeyondUID/TableCfg"
CONTENT_API = "https://ef-webview.hypergryph.com/api/content"
TABLECFG_FILES = ["GachaCharPoolTable.json", "GachaWeaponPoolTable.json"]
CHAR_PORTRAIT_DIR = PUBLIC_IMG_DIR / "character"
WEAPON_PORTRAIT_DIR = PUBLIC_IMG_DIR / "weapon"
BANNER_CHAR_DIR = PUBLIC_IMG_DIR / "banner" / "char"
BANNER_WEAPON_DIR = PUBLIC_IMG_DIR / "banner" / "weapon"

CHAR_PORTRAIT_REMOTE = "https://www.akedata.top/public/images/character/charremoteicon"
WEAPON_PORTRAIT_REMOTE = "https://www.akedata.top/public/images/weapon/icon"

TIMEOUT = 30.0
MAX_CONCURRENCY = 5
ALLOWED_CHAR_POOL_TYPES = {"Special", "Joint", "0", 0}


# ─── TableCfg 同步 ────────────────────────────────────────────────

def sync_tablecfg(client: httpx.Client) -> bool:
    RAW_TABLECFG_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    for filename in TABLECFG_FILES:
        url = f"{TABLECFG_REMOTE_BASE}/{filename}"
        target = RAW_TABLECFG_DIR / filename
        try:
            resp = client.get(url, follow_redirects=True, timeout=TIMEOUT)
            resp.raise_for_status()
            json.loads(resp.text)
            target.write_text(resp.text, encoding="utf-8")
            print(f"  [TableCfg] {filename} updated")
            changed = True
        except Exception as e:
            print(f"  [TableCfg] {filename} sync failed: {e}")
            if target.exists():
                print(f"  [TableCfg] keeping existing {filename}")
    return changed


# ─── 读取 TableCfg ────────────────────────────────────────────────

def load_tablecfg():
    char_table_path = RAW_TABLECFG_DIR / "GachaCharPoolTable.json"
    weapon_table_path = RAW_TABLECFG_DIR / "GachaWeaponPoolTable.json"
    char_table = {}
    weapon_table = {}
    if char_table_path.exists():
        char_table = json.loads(char_table_path.read_text(encoding="utf-8"))
    if weapon_table_path.exists():
        weapon_table = json.loads(weapon_table_path.read_text(encoding="utf-8"))
    return char_table, weapon_table


def collect_pool_ids(char_table: dict, weapon_table: dict) -> list[str]:
    pool_ids = []
    for pool_id, pool_data in char_table.items():
        if pool_data.get("type") in ALLOWED_CHAR_POOL_TYPES:
            pool_ids.append(pool_id)
    for pool_id in weapon_table:
        if pool_id not in pool_ids:
            pool_ids.append(pool_id)
    return pool_ids


# ─── 从官方 API 获取单个池内容 ────────────────────────────────────

def fetch_content_api(client: httpx.Client, pool_id: str) -> dict | None:
    try:
        resp = client.get(
            CONTENT_API,
            params={"pool_id": pool_id, "server_id": "1", "lang": "zh-cn"},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("pool"):
            return data["data"]["pool"]
        return None
    except Exception as e:
        print(f"  [API] {pool_id}: fetch failed: {e}")
        return None


# ─── 下载图片 ──────────────────────────────────────────────────────

def download_image(client: httpx.Client, url: str, target_path: Path) -> bool:
    if not url:
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = client.get(url, follow_redirects=True, timeout=TIMEOUT)
        resp.raise_for_status()
        ext = Path(url.split("?")[0]).suffix or ".png"
        target = target_path.with_suffix(ext)
        target.write_bytes(resp.content)
        return True
    except Exception as e:
        print(f"  [Image] download {url[:60]}... failed: {e}")
        return False


# ─── 合并到历史池表 ──────────────────────────────────────────────

def load_historical_table() -> dict:
    if HISTORICAL_TABLE_PATH.exists():
        return json.loads(HISTORICAL_TABLE_PATH.read_text(encoding="utf-8"))
    return {}


def save_historical_table(table: dict):
    HISTORICAL_TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORICAL_TABLE_PATH.write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ─── 肖像同步 ────────────────────────────────────────────────────

def sync_missing_portraits(client: httpx.Client, table: dict):
    known_char_ids: set[str] = set()
    known_weapon_ids: set[str] = set()

    for pool_id, pool_data in table.items():
        gacha_type = pool_data.get("pool_gacha_type", "")
        for entry in pool_data.get("all", []):
            item_id = entry.get("id", "")
            if gacha_type == "char":
                known_char_ids.add(item_id)
            elif gacha_type == "weapon":
                known_weapon_ids.add(item_id)

    existing_chars = {f.stem for f in CHAR_PORTRAIT_DIR.glob("*") if f.suffix.lower() in (".png", ".webp", ".jpg")}
    existing_weapons = {f.stem for f in WEAPON_PORTRAIT_DIR.glob("*") if f.suffix.lower() in (".png", ".webp", ".jpg")}

    missing_chars = known_char_ids - existing_chars
    missing_weapons = known_weapon_ids - existing_weapons
    downloaded = 0

    for item_id in sorted(missing_chars):
        url = f"{CHAR_PORTRAIT_REMOTE}/icon_{item_id}.png"
        target = CHAR_PORTRAIT_DIR / f"{item_id}.png"
        try:
            resp = client.get(url, follow_redirects=True, timeout=TIMEOUT)
            if resp.status_code == 200:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(resp.content)
                print(f"  [Portrait] 下载角色 {item_id}")
                downloaded += 1
            else:
                print(f"  [Portrait] 角色 {item_id} 不可用 (HTTP {resp.status_code})")
        except Exception as e:
            print(f"  [Portrait] 角色 {item_id} 下载失败: {e}")

    for item_id in sorted(missing_weapons):
        url = f"{WEAPON_PORTRAIT_REMOTE}/{item_id}.png"
        target = WEAPON_PORTRAIT_DIR / f"{item_id}.png"
        try:
            resp = client.get(url, follow_redirects=True, timeout=TIMEOUT)
            if resp.status_code == 200:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(resp.content)
                print(f"  [Portrait] 下载武器 {item_id}")
                downloaded += 1
            else:
                print(f"  [Portrait] 武器 {item_id} 不可用 (HTTP {resp.status_code})")
        except Exception as e:
            print(f"  [Portrait] 武器 {item_id} 下载失败: {e}")

    if downloaded > 0:
        print(f"  本次下载肖像: {downloaded}")
    return downloaded


# ─── 生成版本摘要 ────────────────────────────────────────────────

def generate_index(table: dict):
    pool_count = len(table)
    char_pools = sum(1 for p in table.values() if p.get("pool_gacha_type") == "char")
    weapon_pools = sum(1 for p in table.values() if p.get("pool_gacha_type") == "weapon")
    banner_char_count = len(list(BANNER_CHAR_DIR.glob("*")))
    banner_weapon_count = len(list(BANNER_WEAPON_DIR.glob("*")))
    char_portrait_count = len(list(CHAR_PORTRAIT_DIR.glob("*")))
    weapon_portrait_count = len(list(WEAPON_PORTRAIT_DIR.glob("*")))

    index = {
        "version": datetime.now(timezone.utc).strftime("%Y%m%d"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total_pools": pool_count,
            "char_pools": char_pools,
            "weapon_pools": weapon_pools,
            "banner_char_images": banner_char_count,
            "banner_weapon_images": banner_weapon_count,
            "char_portraits": char_portrait_count,
            "weapon_portraits": weapon_portrait_count,
        },
    }
    index_path = PUBLIC_DATA_DIR / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n  [Index] 已生成")
    return index


# ─── 主流程 ───────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print(f"Endfield Pool Assets Sync - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 50)

    # 同步 TableCfg
    print("\n[1/5] 同步 TableCfg...")
    with httpx.Client() as client:
        sync_tablecfg(client)

    # 读取本地 TableCfg
    print("\n[2/5] 读取 TableCfg...")
    char_table, weapon_table = load_tablecfg()
    pool_ids = collect_pool_ids(char_table, weapon_table)
    print(f"  共收集到 {len(pool_ids)} 个 pool_id")

    # 加载现有历史池表
    historical = load_historical_table()

    # 遍历 pool_id 采集数据
    print("\n[3/5] 采集卡池数据...")
    new_count = 0
    skip_count = 0

    with httpx.Client() as client:
        for pool_id in pool_ids:
            # 已经是历史数据
            if pool_id in historical and historical[pool_id].get("up6_image"):
                skip_count += 1
                continue

            # 尝试官方 API
            pool_data = fetch_content_api(client, pool_id)
            if pool_data:
                historical[pool_id] = pool_data
                new_count += 1
                print(f"  [API] {pool_id}: {pool_data.get('pool_name', '')}")

                # 下载横幅
                up6_image = pool_data.get("up6_image", "") or ""
                gacha_type = pool_data.get("pool_gacha_type", "")
                if up6_image:
                    if gacha_type == "char":
                        download_image(client, up6_image, BANNER_CHAR_DIR / pool_id)
                    elif gacha_type == "weapon":
                        download_image(client, up6_image, BANNER_WEAPON_DIR / pool_id)

                # 轮换图已移除，不再下载
            else:
                print(f"  [跳过] {pool_id}: API 不可用")

    print(f"\n  新增: {new_count}, 已存在: {skip_count}")

    # 保存合并后的数据
    print("\n[4/5] 保存数据...")
    save_historical_table(historical)

    # 检测并下载缺失肖像
    print("\n[5/5] 同步肖像...")
    with httpx.Client() as client:
        sync_missing_portraits(client, historical)

    # 生成索引（肖像文件已更新时包含最新计数）
    generate_index(historical)

    print("\n" + "=" * 50)
    print("同步完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
