"""Store mail app passwords in macOS Keychain, never config files."""

import Security


SERVICE = "no.olegrimstad.macassistant.icloud"


def _query(account, provider):
    if provider not in ("icloud", "gmail"):
        raise ValueError("Choose Gmail or iCloud in Preferences → Mail.")
    return {Security.kSecClass: Security.kSecClassGenericPassword,
            Security.kSecAttrService: SERVICE if provider == "icloud" else "no.olegrimstad.macassistant.gmail",
            Security.kSecAttrAccount: account.strip().lower()}


def normalize_password(password, provider):
    return "".join(password.split()) if provider == "gmail" else password.strip()


def _check(status):
    if status != Security.errSecSuccess:
        raise RuntimeError(f"macOS Keychain access failed ({status}). Unlock your login Keychain and retry.")


def get_password(account, provider="icloud"):
    status, data = Security.SecItemCopyMatching({**_query(account, provider), Security.kSecReturnData: True,
                                               Security.kSecMatchLimit: Security.kSecMatchLimitOne}, None)
    if status == Security.errSecItemNotFound:
        return None
    _check(status)
    return bytes(data).decode("utf-8")


def save_password(account, password, provider="icloud"):
    password = normalize_password(password, provider)
    if not account.strip() or not password:
        raise ValueError("Enter your email address and app password.")
    data = password.encode("utf-8")
    status = Security.SecItemUpdate(_query(account, provider), {Security.kSecValueData: data})
    if status == Security.errSecItemNotFound:
        status, _ = Security.SecItemAdd({**_query(account, provider), Security.kSecValueData: data}, None)
    _check(status)


def delete_password(account, provider="icloud"):
    status = Security.SecItemDelete(_query(account, provider))
    if status != Security.errSecItemNotFound:
        _check(status)
