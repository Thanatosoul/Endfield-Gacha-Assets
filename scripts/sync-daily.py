# sync-daily.py
# Endfield Pool Assets 每日同步脚本
# 1. 同步远端 TableCfg
# 2. 遍历 pool_id，新池走官方 API，旧池回填历史数据
# 3. 下载横幅/轮换图
# 4. 合并到 GachaPoolTable.json
# 5. 检测缺失肖像

import json
import re
from pathlib import Path
from datetime import datetime, timezone

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DATA_DIR = PUBLIC_DIR / "data"
PUBLIC_IMG_DIR = PUBLIC_DIR / "images"
RAW_TABLECFG_DIR = BASE_DIR / "raw" / "TableCfg"
HISTORICAL_TABLE_PATH = PUBLIC_DATA_DIR / "GachaPoolTable.json"
PENDING_POOLS_PATH = PUBLIC_DATA_DIR / "pending-pools.json"

TABLECFG_REMOTE_BASE = "https://lulush.microgg.cn/BeyondUID/TableCfg"
CONTENT_API = "https://ef-webview.hypergryph.com/api/content"
TABLECFG_FILES = ["GachaCharPoolTable.json", "GachaWeaponPoolTable.json"]
CHAR_PORTRAIT_DIR = PUBLIC_IMG_DIR / "character"
WEAPON_PORTRAIT_DIR = PUBLIC_IMG_DIR / "weapon"
BANNER_CHAR_DIR = PUBLIC_IMG_DIR / "banner" / "char"
BANNER_WEAPON_DIR = PUBLIC_IMG_DIR / "banner" / "weapon"

CHAR_PORTRAIT_REMOTE = "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/charremoteicon"
WEAPON_PORTRAIT_REMOTE = "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/itemiconbig"

# 第三方镜像：官方 content 接口对部分卡池（如联合池）不返回图片时作为后备来源。
IVAQAIS_BANNER_REMOTE = "https://raw.githubusercontent.com/ivaqis/arknights-tracker/main/arknights-tracker/static/images/banners/icon"
CEP_BANNER_REMOTE = "https://raw.githubusercontent.com/cmyyx/cep/main/public/images/banners"
CEP_BANNER_SLUGS = {"joint_1_2_2": "huiguagnqingdian"}
CEP_CHARACTER_PORTRAIT_REMOTE = "https://raw.githubusercontent.com/cmyyx/cep/main/public/images/characters"
CEP_WEAPON_PORTRAIT_REMOTE = "https://raw.githubusercontent.com/cmyyx/cep/main/public/images/weapon"
CEP_WEAPON_PORTRAIT_INDEX = "https://api.github.com/repos/cmyyx/cep/contents/public/images/weapon"
ENDFIELDDATA_WEAPON_TABLE = "https://raw.githubusercontent.com/rmxlinux/EndfieldData/main/TableCfg/GachaWeaponPoolTable.json"
ENDFIELDDATA_CHAR_TABLE = "https://raw.githubusercontent.com/rmxlinux/EndfieldData/main/TableCfg/GachaCharPoolTable.json"
ENDFIELDDATA_COMMITS = "https://api.github.com/repos/rmxlinux/EndfieldData/commits?path=TableCfg/GachaWeaponPoolTable.json&per_page=1"
AKEDATA_MANIFEST = "https://data.akedata.wiki/manifest.json"

TIMEOUT = 30.0
ALLOWED_CHAR_POOL_TYPES = {"Special", "Joint", "Rerun", "0", 0}


def pool_kind_for(pool_id: str, pool_data: dict, char_table: dict, weapon_table: dict) -> str:
    """Persist product-facing pool categories alongside the raw API type."""
    if pool_id in weapon_table or pool_data.get("pool_gacha_type") == "weapon":
        return "weapon"

    configured_type = str(char_table.get(pool_id, {}).get("type", "")).lower()
    if configured_type in {"beginner", "standard", "special", "joint", "rerun"}:
        return configured_type

    pool_id_lower = pool_id.lower()
    if "rerun" in pool_id_lower or "reprint" in pool_id_lower:
        return "rerun"
    if pool_id_lower.startswith("joint") or pool_data.get("pool_type") == "extra":
        return "joint"
    if pool_id_lower == "beginner" or pool_id_lower.startswith("beginner"):
        return "beginner"
    if pool_id_lower == "standard" or pool_id_lower.startswith("standard"):
        return "standard"
    return "special"


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
            if not target.exists() or target.read_text(encoding="utf-8") != resp.text:
                target.write_text(resp.text, encoding="utf-8")
                print(f"  [TableCfg] {filename} updated")
                changed = True
            else:
                print(f"  [TableCfg] {filename} unchanged")
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


def fetch_json(client: httpx.Client, url: str):
    resp = client.get(url, follow_redirects=True, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def load_supplemental_tables(client: httpx.Client) -> list[tuple[str, str, str, str, dict]]:
    """Load public TableCfg snapshots as discovery sources, newest first."""
    sources: list[tuple[str, str, str, str, dict]] = []
    try:
        commit = fetch_json(client, ENDFIELDDATA_COMMITS)[0]
        updated_at = commit["commit"]["author"]["date"]
        for table_name, url in (
            ("weapon", ENDFIELDDATA_WEAPON_TABLE),
            ("char", ENDFIELDDATA_CHAR_TABLE),
        ):
            table = fetch_json(client, url)
            sources.append((updated_at, "rmxlinux/EndfieldData", table_name, "main", table))
        print(f"  [Source] EndfieldData TableCfg @ {updated_at}")
    except Exception as e:
        print(f"  [Source] EndfieldData unavailable: {e}")

    try:
        manifest = fetch_json(client, AKEDATA_MANIFEST)
        version = next(row for row in manifest["versions"] if row["id"] == manifest["latest"])
        updated_at = version.get("publishedAt") or manifest.get("updatedAt") or ""
        for table_name, filename in (
            ("weapon", "GachaWeaponPoolTable.json"),
            ("char", "GachaCharPoolTable.json"),
        ):
            url = f"https://data.akedata.wiki/{version['tableCfgPath']}/{filename}"
            table = fetch_json(client, url)
            sources.append((updated_at, "NagiYume/AKEDatabase", table_name, version["id"], table))
        print(f"  [Source] AKEData {version['id']} @ {updated_at}")
    except Exception as e:
        print(f"  [Source] AKEData unavailable: {e}")

    return sorted(sources, key=lambda source: source[0], reverse=True)


def make_pending_pool(
    pool_id: str,
    config: dict,
    gacha_type: str,
    source: str,
    source_version: str,
    updated_at: str,
) -> dict:
    """Keep the source config intact without presenting it as official pool content."""
    return {
        "pool_id": pool_id,
        "pool_gacha_type": gacha_type,
        "data_status": "pending_official",
        "data_source": source,
        "source_version": source_version,
        "source_updated_at": updated_at,
        "tablecfg": config,
    }


def merge_supplemental_tables(
    sources: list[tuple[str, str, str, str, dict]],
) -> tuple[dict[str, tuple[dict, str, str, str, str]], dict, dict]:
    """Choose the newest config per pool and keep character/weapon maps for classification."""
    configs: dict[str, tuple[dict, str, str, str, str]] = {}
    char_table: dict = {}
    weapon_table: dict = {}

    for updated_at, source, table_kind, source_version, table in sources:
        target_table = char_table if table_kind == "char" else weapon_table
        for pool_id, config in table.items():
            target_table[pool_id] = config
            current = configs.get(pool_id)
            if current is None or updated_at > current[1]:
                configs[pool_id] = (config, updated_at, source, table_kind, source_version)
            elif updated_at == current[1] and source != current[2]:
                merged = {**current[0], **config}
                configs[pool_id] = (
                    merged,
                    updated_at,
                    f"{current[2]},{source}",
                    table_kind,
                    f"{current[4]},{source_version}",
                )

    return configs, char_table, weapon_table


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


def sync_pool_banner(client: httpx.Client, pool_id: str, pool_data: dict, banner_dir: Path) -> bool:
    """Download the official banner first, then try configured mirrors."""
    image_url = pool_data.get("up6_image", "") or ""
    if image_url and download_image(client, image_url, banner_dir / pool_id):
        return True

    has_banner = any(
        (banner_dir / f"{pool_id}{ext}").exists() for ext in (".png", ".webp", ".jpg")
    )
    if has_banner:
        return True

    for fallback_url in banner_fallback_urls(pool_id):
        if download_image(client, fallback_url, banner_dir / pool_id):
            print(f"  [Banner] mirror download {pool_id}")
            return True
    return False


# ─── 合并到历史池表 ──────────────────────────────────────────────

def load_historical_table() -> dict:
    if HISTORICAL_TABLE_PATH.exists():
        return json.loads(HISTORICAL_TABLE_PATH.read_text(encoding="utf-8"))
    return {}


def load_pending_pools() -> dict:
    if PENDING_POOLS_PATH.exists():
        return json.loads(PENDING_POOLS_PATH.read_text(encoding="utf-8"))
    return {}


def save_historical_table(table: dict):
    HISTORICAL_TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORICAL_TABLE_PATH.write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def save_pending_pools(table: dict):
    PENDING_POOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_POOLS_PATH.write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def persist_pending_pools(table: dict) -> bool:
    """Write pending candidates only when changed; remove the file when empty."""
    if table == load_pending_pools():
        return False
    if table:
        save_pending_pools(table)
    elif PENDING_POOLS_PATH.exists():
        PENDING_POOLS_PATH.unlink()
    return True


# ─── 图片后备源 ──────────────────────────────────────────────────

def banner_fallback_urls(pool_id: str) -> list[str]:
    """第三方横幅镜像：官方无图时按顺序尝试。"""
    urls: list[str] = []
    slug = CEP_BANNER_SLUGS.get(pool_id)
    if slug:
        urls.append(f"{CEP_BANNER_REMOTE}/{slug}.webp")
    urls.append(f"{IVAQAIS_BANNER_REMOTE}/{pool_id}.webp")
    return urls


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

    _, weapon_table = load_tablecfg()
    for pool_data in weapon_table.values():
        known_weapon_ids.update(
            item_id for item_id in pool_data.get("upWeaponIds", []) if item_id
        )

    try:
        resp = client.get(
            CEP_WEAPON_PORTRAIT_INDEX,
            headers={"User-Agent": "Endfield-Gacha-Assets-sync"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        candidate_weapons = set()
        for entry in resp.json():
            filename = entry.get("name", "")
            match = re.fullmatch(r"(wpn_[a-z]+_\d+)\.avif", filename)
            if match:
                candidate_weapons.add(match.group(1))

        max_known_number: dict[str, int] = {}
        for item_id in known_weapon_ids:
            match = re.fullmatch(r"(wpn_[a-z]+)_(\d+)", item_id)
            if match:
                weapon_type, number = match.groups()
                max_known_number[weapon_type] = max(
                    max_known_number.get(weapon_type, 0), int(number)
                )

        for item_id in candidate_weapons:
            match = re.fullmatch(r"(wpn_[a-z]+)_(\d+)", item_id)
            if match and int(match.group(2)) > max_known_number.get(match.group(1), 0):
                known_weapon_ids.add(item_id)
    except Exception as e:
        print(f"  [Portrait] 无法读取 CEP 武器清单: {e}")

    existing_chars = {f.stem for f in CHAR_PORTRAIT_DIR.glob("*") if f.suffix.lower() in (".png", ".webp", ".jpg")}
    existing_weapons = {f.stem for f in WEAPON_PORTRAIT_DIR.glob("*") if f.suffix.lower() in (".png", ".webp", ".jpg")}

    missing_chars = known_char_ids - existing_chars
    missing_weapons = known_weapon_ids - existing_weapons
    downloaded = 0

    for item_id in sorted(missing_chars):
        candidates = [
            f"{CHAR_PORTRAIT_REMOTE}/icon_{item_id}.png",
            f"{CEP_CHARACTER_PORTRAIT_REMOTE}/{item_id}.avif",
        ]
        if any(download_image(client, url, CHAR_PORTRAIT_DIR / item_id) for url in candidates):
            print(f"  [Portrait] 下载角色 {item_id}")
            downloaded += 1

    for item_id in sorted(missing_weapons):
        candidates = [
            f"{WEAPON_PORTRAIT_REMOTE}/{item_id}.png",
            f"{CEP_WEAPON_PORTRAIT_REMOTE}/{item_id}.avif",
        ]
        if any(download_image(client, url, WEAPON_PORTRAIT_DIR / item_id) for url in candidates):
            print(f"  [Portrait] 下载武器 {item_id}")
            downloaded += 1

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
        tablecfg_changed = sync_tablecfg(client)

    # 读取本地 TableCfg
    print("\n[2/5] 读取 TableCfg...")
    char_table, weapon_table = load_tablecfg()
    with httpx.Client() as client:
        supplemental_tables = load_supplemental_tables(client)

    supplemental_configs, supplemental_chars, supplemental_weapons = merge_supplemental_tables(
        supplemental_tables
    )
    pool_ids = list(dict.fromkeys(
        collect_pool_ids(char_table, weapon_table)
        + collect_pool_ids(supplemental_chars, supplemental_weapons)
        + list(supplemental_configs)
    ))
    tablecfg_pool_count = len(pool_ids)

    # Load official pool records separately from unconfirmed source candidates.
    historical = load_historical_table()
    pending_pools = load_pending_pools()
    pool_kind_changed = False
    for pool_id, pool_data in historical.items():
        pool_kind = pool_kind_for(pool_id, pool_data, char_table, weapon_table)
        if pool_data.get("pool_kind") != pool_kind:
            pool_data["pool_kind"] = pool_kind
            pool_kind_changed = True

    print(f"  TableCfg: {tablecfg_pool_count} 个 pool_id")
    print(f"  待查池 ID: {len(pool_ids)} 个")

    # 遍历 pool_id 采集数据
    print("\n[3/5] 采集卡池数据...")
    new_count = 0
    skip_count = 0

    with httpx.Client() as client:
        for pool_id in pool_ids:
            if pool_id in historical and historical[pool_id].get("data_status") != "pending_official":
                skip_count += 1
                continue

            # 尝试官方 API
            pool_data = fetch_content_api(client, pool_id)
            if pool_data:
                pool_kind = pool_kind_for(pool_id, pool_data, char_table, weapon_table)
                if historical.get(pool_id, {}).get("pool_kind") != pool_kind:
                    pool_kind_changed = True
                pool_data["pool_kind"] = pool_kind
                historical[pool_id] = pool_data
                pending_pools.pop(pool_id, None)
                new_count += 1
                print(f"  [API] {pool_id}: {pool_data.get('pool_name', '')}")

                gacha_type = pool_data.get("pool_gacha_type", "")
                banner_dir = {
                    "char": BANNER_CHAR_DIR,
                    "weapon": BANNER_WEAPON_DIR,
                }.get(gacha_type)
                if banner_dir:
                    sync_pool_banner(client, pool_id, pool_data, banner_dir)

                # 轮换图已移除，不再下载
            else:
                supplemental = supplemental_configs.get(pool_id)
                if supplemental and pool_id not in historical:
                    config, updated_at, source, table_kind, source_version = supplemental
                    gacha_type = "weapon" if table_kind == "weapon" else "char"
                    pending = make_pending_pool(
                        pool_id, config, gacha_type, source, source_version, updated_at
                    )
                    pending_pools[pool_id] = pending
                    if gacha_type == "weapon" and re.fullmatch(
                        r"(?:weponbox|weaponbox)_\d+_\d+_\d+", pool_id
                    ):
                        sync_pool_banner(client, pool_id, {}, BANNER_WEAPON_DIR)
                    print(f"  [Pending] {pool_id}: {source}, waiting for official API")
                else:
                    print(f"  [跳过] {pool_id}: API 不可用")

    print(f"\n  新增: {new_count}, 已存在: {skip_count}")

    # 保存合并后的数据
    print("\n[4/5] 保存数据...")
    if new_count or pool_kind_changed:
        save_historical_table(historical)
    else:
        print("  卡池数据未变化")
    pending_changed = persist_pending_pools(pending_pools)

    # 检测并下载缺失肖像
    print("\n[5/5] 同步肖像...")
    with httpx.Client() as client:
        portrait_count = sync_missing_portraits(client, historical)

    # 只在数据或资源发生变化时更新时间戳，避免每日产生无意义的提交。
    if tablecfg_changed or new_count or pending_changed or portrait_count:
        generate_index(historical)
    else:
        print("\n  [Index] 无数据变化，未更新")

    print("\n" + "=" * 50)
    print("同步完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
