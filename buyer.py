"""
محرك الشراء التلقائي المتعدد المحافظ.
يبني معاملة الشراء عبر واجهة OpenSea الرسمية (POST /api/v2/drops/{slug}/mint) التي
تختار المرحلة المؤهلة تلقائيًا (عامة أو Allowlist) وتتولى Merkle Proof بنفسها.
"""

import asyncio
import logging
import requests
from web3 import Web3

log = logging.getLogger("buyer")

MIN_BALANCE_RESERVE_USD = 0.10
FEW_THRESHOLD = 20
LIMITED_BUY_QTY = 15
GAS_LIMIT_SAFETY_MARGIN = 1.2
FREE_PRICE_THRESHOLD_USD = 0.01
OPENSEA_MINT_BUILD_URL = "https://api.opensea.io/api/v2/drops/{slug}/mint"

# ---------------------------------------------------------------------------
# فك ترميز أخطاء العقد (Custom Errors) بدل عرض hex خام غير مقروء
# ---------------------------------------------------------------------------

# التوقيعات مأخوذة من الكود المصدري الرسمي لـ SeaDrop (ProjectOpenSea/seadrop)
SEADROP_ERROR_SIGNATURES = {
    "MintQuantityCannotBeZero()": "الكمية المطلوبة صفر",
    "MintQuantityExceedsMaxMintedPerWallet(uint256,uint256)": "تجاوزت الكمية الحد المسموح لهذه المحفظة",
    "MintQuantityExceedsMaxSupply(uint256,uint256)": "نفدت الكمية المتاحة (Max Supply)",
    "MintQuantityExceedsMaxTokenSupplyForStage(uint256,uint256)": "نفدت الكمية المتاحة لهذه المرحلة",
    "NotActive(uint256,uint256,uint256)": "المرحلة غير نشطة حاليًا (لم تبدأ أو انتهت)",
    "IncorrectPayment(uint256,uint256)": "قيمة الدفع المُرسلة غير صحيحة",
    "PublicDropStageNotPresent()": "لا توجد مرحلة عامة (Public Drop) لهذا العقد",
}


def _build_selector_map() -> dict:
    mapping = {}
    for sig, arabic in SEADROP_ERROR_SIGNATURES.items():
        try:
            selector = Web3.keccak(text=sig)[:4].hex()
            if not selector.startswith("0x"):
                selector = "0x" + selector
            mapping[selector.lower()] = (sig, arabic)
        except Exception:
            pass
    return mapping


SEADROP_ERROR_SELECTORS = _build_selector_map()


def decode_web3_error(e: Exception) -> str:
    """يحاول تحويل استثناء web3/RPC غير المقروء إلى رسالة عربية مختصرة ومفهومة."""
    import re

    # الحالة 1: استثناء JSON-RPC عادي يحمل dict فيه 'message' (الأشيع لأخطاء الشبكة/الرصيد)
    if e.args and isinstance(e.args[0], dict):
        msg = e.args[0].get("message")
        if msg:
            return str(msg)[:200]

    text = str(e)

    # الحالة 2: مطابقة أول 4 بايت من أي بيانات hex ضد أخطاء SeaDrop المعروفة
    match = re.search(r"0x[0-9a-fA-F]{8,}", text)
    if match:
        hex_data = match.group(0).lower()
        selector = hex_data[:10]
        known = SEADROP_ERROR_SELECTORS.get(selector)
        if known:
            return f"رفض من العقد: {known[1]}"
        short = hex_data[:14] + ("…" if len(hex_data) > 14 else "")
        return f"رفض من العقد (سبب غير معروف لدينا، selector: {short})"

    # الحالة 3: أي نص آخر — يُقصّ لتفادي إغراق اللوج
    return text[:200]

# قفل خاص لكل محفظة لمنع تضارب المعاملات والنونس في نفس الوقت
wallet_locks = {}

def get_wallet_lock(wallet_address: str) -> asyncio.Lock:
    addr = wallet_address.lower()
    if addr not in wallet_locks:
        wallet_locks[addr] = asyncio.Lock()
    return wallet_locks[addr]


def get_web3(rpc_url: str) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url))


def get_wallet_balance_usd(w3: Web3, wallet_address: str, eth_price_usd: float) -> float:
    try:
        checksum_wallet = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(checksum_wallet)
        return (balance_wei / 1e18) * eth_price_usd
    except Exception as e:
        log.error(f"[الرصيد] تعذر القراءة للمحفظة {wallet_address[:8]}...: {e}")
        return 0.0


def estimate_gas_fee_usd(w3: Web3, eth_price_usd: float, gas_units: int = 150_000) -> float:
    try:
        gas_price_wei = w3.eth.gas_price
        fee_eth = (gas_price_wei * gas_units) / 1e18
        return fee_eth * eth_price_usd
    except Exception as e:
        log.warning(f"[الغاز] تعذر التقدير: {e}")
        return float("inf")


def build_mint_tx_via_opensea(slug: str, opensea_api_key: str, minter: str, quantity: int) -> dict:
    """
    يطلب من OpenSea بناء معاملة شراء جاهزة للتوقيع. OpenSea تختار المرحلة المؤهلة
    تلقائيًا (عامة أو Allowlist) لهذه المحفظة تحديدًا، وتتولى Merkle Proof بنفسها —
    لا حاجة لأي منطق يدوي لتحديد المرحلة أو الأهلية من طرفنا.
    """
    try:
        url = OPENSEA_MINT_BUILD_URL.format(slug=slug)
        headers = {"x-api-key": opensea_api_key, "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json={"minter": minter, "quantity": quantity}, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            value_raw = str(data.get("value", "0"))
            value_wei = int(value_raw, 16) if value_raw.lower().startswith("0x") else int(value_raw)
            return {
                "ok": True,
                "to": Web3.to_checksum_address(data["to"]),
                "data": data["data"],
                "value": value_wei,
            }

        try:
            errors = resp.json().get("errors", []) or []
        except Exception:
            errors = []
        error_text = "; ".join(errors) if errors else resp.text[:200]
        limit_exceeded = any("limit" in e.lower() for e in errors) or "limit" in error_text.lower()

        return {
            "ok": False,
            "status": resp.status_code,
            "error_text": error_text,
            "limit_exceeded": limit_exceeded,
        }
    except Exception as e:
        return {"ok": False, "status": None, "error_text": str(e)[:200], "limit_exceeded": False}


def decide_quantity(max_per_wallet: int | None, remaining_supply: int) -> int:
    if max_per_wallet is None:
        qty = 5
    elif max_per_wallet <= FEW_THRESHOLD:
        qty = max_per_wallet
    else:
        qty = LIMITED_BUY_QTY
    return max(1, min(qty, remaining_supply))


def attempt_purchase_single_wallet(
    w3: Web3,
    private_key: str,
    wallet_address: str,
    max_per_wallet: int | None,
    remaining_supply: int,
    eth_price_usd: float,
    max_gas_fee_usd: float,
    slug: str,
    opensea_api_key: str,
) -> dict:
    """محاولة الشراء بمحفظة واحدة محددة، عبر معاملة يبنيها OpenSea (تختار المرحلة
    المؤهلة تلقائيًا — عامة أو Allowlist — وتضمّن Merkle Proof إن لزم)."""
    try:
        checksum_wallet = Web3.to_checksum_address(wallet_address)
    except Exception as e:
        log.error(f"[{slug} | {wallet_address[:8]}] عنوان غير صالح: {e}")
        return {"success": False, "wallet": wallet_address, "reason": "invalid_address", "error": str(e)}

    balance_usd = get_wallet_balance_usd(w3, checksum_wallet, eth_price_usd)
    if balance_usd < MIN_BALANCE_RESERVE_USD:
        log.warning(
            f"[{slug} | {checksum_wallet[:8]}] ⏭️ رصيد غير كافٍ: "
            f"${balance_usd:.2f} < الحد الأدنى ${MIN_BALANCE_RESERVE_USD:.2f}"
        )
        return {"success": False, "wallet": checksum_wallet, "reason": "balance_too_low", "balance_usd": balance_usd}

    gas_fee_usd = estimate_gas_fee_usd(w3, eth_price_usd)
    if gas_fee_usd > max_gas_fee_usd:
        log.warning(
            f"[{slug} | {checksum_wallet[:8]}] ⏭️ رسوم الغاز مرتفعة (تقدير أولي): "
            f"${gas_fee_usd:.4f} > الحد الأقصى ${max_gas_fee_usd:.4f}"
        )
        return {"success": False, "wallet": checksum_wallet, "reason": "gas_too_high", "gas_fee_usd": gas_fee_usd}

    # طلب بناء معاملة الشراء من OpenSea — تختار المرحلة المؤهلة لهذه المحفظة تلقائيًا
    quantity = decide_quantity(max_per_wallet, remaining_supply)
    mint_build = build_mint_tx_via_opensea(slug, opensea_api_key, checksum_wallet, quantity)

    if not mint_build["ok"] and mint_build.get("limit_exceeded") and quantity != 1:
        log.info(
            f"[{slug} | {checksum_wallet[:8]}] الكمية المطلوبة ({quantity}) تتجاوز الحد المسموح "
            f"لهذه المحفظة — إعادة المحاولة بكمية 1."
        )
        quantity = 1
        mint_build = build_mint_tx_via_opensea(slug, opensea_api_key, checksum_wallet, quantity)

    if not mint_build["ok"]:
        status = mint_build.get("status")
        error_text = mint_build.get("error_text", "")
        if status == 422:
            reason = "not_eligible_or_sold_out"
        elif status == 409:
            reason = "stage_not_active"
        else:
            reason = "mint_build_failed"
        log.warning(f"[{slug} | {checksum_wallet[:8]}] ⏭️ تعذر بناء معاملة الشراء (HTTP {status}): {error_text}")
        return {"success": False, "wallet": checksum_wallet, "reason": reason, "error": error_text}

    total_value = mint_build["value"]

    # تحقق أمان أخير: المرحلة التي اختارتها OpenSea لهذه المحفظة يجب أن تكون مجانية فعلاً
    price_usd_for_this = (total_value / 1e18) * eth_price_usd
    if price_usd_for_this >= FREE_PRICE_THRESHOLD_USD:
        log.warning(
            f"[{slug} | {checksum_wallet[:8]}] ⏭️ المرحلة المؤهلة لهذه المحفظة ليست مجانية "
            f"(${price_usd_for_this:.4f}) — تخطي."
        )
        return {"success": False, "wallet": checksum_wallet, "reason": "stage_not_free_for_wallet"}

    try:
        nonce = w3.eth.get_transaction_count(checksum_wallet, "pending")
        tx = {
            "from": checksum_wallet,
            "to": mint_build["to"],
            "data": mint_build["data"],
            "value": total_value,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        }

        try:
            estimated_gas = w3.eth.estimate_gas(tx)
            tx["gas"] = int(estimated_gas * GAS_LIMIT_SAFETY_MARGIN)
        except Exception as e:
            readable = decode_web3_error(e)
            log.warning(f"[{slug} | {checksum_wallet[:8]}] ⏭️ فشلت محاكاة المعاملة: {readable}")
            return {"success": False, "wallet": checksum_wallet, "reason": "simulation_failed", "error": readable}

        actual_gas_fee_usd = (tx["gas"] * w3.eth.gas_price / 1e18) * eth_price_usd
        if actual_gas_fee_usd > max_gas_fee_usd:
            log.warning(
                f"[{slug} | {checksum_wallet[:8]}] ⏭️ رسوم الغاز مرتفعة (بعد المحاكاة): "
                f"${actual_gas_fee_usd:.4f} > الحد الأقصى ${max_gas_fee_usd:.4f}"
            )
            return {"success": False, "wallet": checksum_wallet, "reason": "gas_too_high", "gas_fee_usd": actual_gas_fee_usd}

        total_cost_wei = total_value + (tx["gas"] * w3.eth.gas_price)
        wallet_balance_wei = w3.eth.get_balance(checksum_wallet)
        if wallet_balance_wei < total_cost_wei:
            log.warning(
                f"[{slug} | {checksum_wallet[:8]}] ⏭️ الرصيد لا يغطي (سعر المينت + الغاز): "
                f"متوفر {wallet_balance_wei} wei < مطلوب {total_cost_wei} wei"
            )
            return {"success": False, "wallet": checksum_wallet, "reason": "insufficient_funds_for_total_cost"}

        signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        log.info(f"[{slug} | شراء ناجح - {checksum_wallet[:8]}] {tx_hash.hex()} — كمية: {quantity}")
        return {
            "success": True,
            "wallet": checksum_wallet,
            "tx_hash": tx_hash.hex(),
            "quantity": quantity,
            "gas_fee_usd": actual_gas_fee_usd,
            "total_value_wei": total_value,
        }

    except Exception as e:
        readable = decode_web3_error(e)
        log.error(f"[{slug} | خطأ إرسال للمحفظة {checksum_wallet[:8]}] {readable}")
        return {"success": False, "wallet": checksum_wallet, "reason": "tx_error", "error": readable}

