"""
محرك الشراء التلقائي المتعدد المحافظ عبر عقد SeaDrop.
"""

import asyncio
import logging
from web3 import Web3

log = logging.getLogger("buyer")

SEADROP_ADDRESS = Web3.to_checksum_address("0x00005EA00Ac477B1030CE78506496e8C2dE24bf5")
ZERO_ADDRESS = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")

SEADROP_ABI = [
    {
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "feeRecipient", "type": "address"},
            {"name": "minterIfNotPayer", "type": "address"},
            {"name": "quantity", "type": "uint256"},
        ],
        "name": "mintPublic",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "nftContract", "type": "address"}],
        "name": "getAllowedFeeRecipients",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "nftContract", "type": "address"}],
        "name": "getPublicDrop",
        "outputs": [{
            "components": [
                {"name": "mintPrice", "type": "uint80"},
                {"name": "startTime", "type": "uint48"},
                {"name": "endTime", "type": "uint48"},
                {"name": "maxTotalMintableByWallet", "type": "uint16"},
                {"name": "feeBps", "type": "uint16"},
                {"name": "restrictFeeRecipients", "type": "bool"},
            ],
            "name": "",
            "type": "tuple",
        }],
        "stateMutability": "view",
        "type": "function",
    },
]

MIN_BALANCE_RESERVE_USD = 0.10
FEW_THRESHOLD = 20
LIMITED_BUY_QTY = 15
GAS_LIMIT_SAFETY_MARGIN = 1.2

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


def get_fee_recipient(w3: Web3, nft_contract: str) -> str | None:
    try:
        seadrop = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
        recipients = seadrop.functions.getAllowedFeeRecipients(
            Web3.to_checksum_address(nft_contract)
        ).call()
        if not recipients:
            return None
        return Web3.to_checksum_address(recipients[0])
    except Exception as e:
        log.error(f"[عنوان الرسوم] خطأ استعلام: {e}")
        return None


def decide_quantity(max_per_wallet: int | None, remaining_supply: int) -> int:
    if max_per_wallet is None:
        qty = 5
    elif max_per_wallet <= FEW_THRESHOLD:
        qty = max_per_wallet
    else:
        qty = LIMITED_BUY_QTY
    return max(1, min(qty, remaining_supply))


def get_onchain_public_price_wei(w3: Web3, nft_contract: str) -> int | None:
    try:
        seadrop = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
        public_drop = seadrop.functions.getPublicDrop(
            Web3.to_checksum_address(nft_contract)
        ).call()
        return int(public_drop[0])
    except Exception as e:
        log.warning(f"[سعر on-chain] تعذر القراءة: {e}")
        return None


def attempt_purchase_single_wallet(
    w3: Web3,
    private_key: str,
    wallet_address: str,
    nft_contract: str,
    price_wei_per_token: int,
    max_per_wallet: int | None,
    remaining_supply: int,
    eth_price_usd: float,
    max_gas_fee_usd: float,
    slug: str = "?",
) -> dict:
    """محاولة الشراء بمحفظة واحدة محددة"""
    try:
        checksum_wallet = Web3.to_checksum_address(wallet_address)
        checksum_contract = Web3.to_checksum_address(nft_contract)
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

    fee_recipient = get_fee_recipient(w3, checksum_contract)
    if not fee_recipient:
        log.warning(f"[{slug} | {checksum_wallet[:8]}] ⏭️ تعذر جلب fee_recipient من العقد.")
        return {"success": False, "wallet": checksum_wallet, "reason": "no_fee_recipient"}

    quantity = decide_quantity(max_per_wallet, remaining_supply)
    total_value = price_wei_per_token * quantity

    try:
        contract = w3.eth.contract(address=SEADROP_ADDRESS, abi=SEADROP_ABI)
        nonce = w3.eth.get_transaction_count(checksum_wallet, "pending")

        tx = contract.functions.mintPublic(
            checksum_contract,
            Web3.to_checksum_address(fee_recipient),
            ZERO_ADDRESS,
            quantity,
        ).build_transaction({
            "from": checksum_wallet,
            "value": total_value,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })

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

