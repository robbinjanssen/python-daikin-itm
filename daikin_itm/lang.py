"""The words the iTM uses, in the languages it can show.

Everything the client needs is addressed by part ID, icon ID or index, except a few
labels the iTM only sends as text in its display language: the operation modes, the
wall remote locks, the on/off and setback words of schedule event texts, the status
lines on the Details screen, an analog point's attributes, web user types and the
name of the built-in "All" zone. The tables here map those labels; lookups try every
language, so nothing has to be configured.

The entries were captured from a device in each of the nine languages its language
screen offers (nl, en, de, es, pt, ja, fr, it, zh). Labels that device never showed
(dry mode, stop-only in event texts, a few "on" values and lower limits) follow the
captured ones and are marked inferred. Korean is in the web UI but was not offered.
An unknown label is reported by the caller (see ``Capabilities.unknown_labels``),
never silently ignored. Lookups ignore case and outer spaces (German pads some
labels with a space).
"""

from __future__ import annotations

# Operation mode labels -> heat / cool / fan / dry / auto. Fan, cool and heat are the
# settings dialog's; auto comes from schedule event texts; dry is inferred.
MODE_LABELS: dict[str, dict[str, str]] = {
    "nl": {
        "verwarmen": "heat",
        "koelen": "cool",
        "ventilator": "fan",
        "ventilatie": "fan",
        "drogen": "dry",
        "automatisch": "auto",
        "auto": "auto",
    },
    "en": {
        "heat": "heat",
        "cool": "cool",
        "fan": "fan",
        "dry": "dry",
        "automatic": "auto",
        "auto": "auto",
    },
    "de": {
        "heizen": "heat",
        "kühlen": "cool",
        "umluft": "fan",
        "entfeuchten": "dry",
        "trocknen": "dry",
        "automatik": "auto",
    },
    "es": {
        "calor": "heat",
        "frío": "cool",
        "ventilación": "fan",
        "seco": "dry",
        "deshumidificación": "dry",
        "auto": "auto",
    },
    "pt": {
        "calor": "heat",
        "frio": "cool",
        "ventilação": "fan",
        "desumidificação": "dry",
        "seco": "dry",
        "auto": "auto",
    },
    "ja": {
        "暖房": "heat",
        "冷房": "cool",
        "送風": "fan",
        "除湿": "dry",
        "ドライ": "dry",
        "自動": "auto",
    },
    "fr": {
        "chauffage": "heat",
        "rafraîchir": "cool",
        "ventilation": "fan",
        "déshumidification": "dry",
        "auto": "auto",
    },
    "it": {
        "caldo": "heat",
        "freddo": "cool",
        "vent": "fan",
        "deumidificazione": "dry",
        "auto": "auto",
    },
    "zh": {
        "制热": "heat",
        "制冷": "cool",
        "通风": "fan",
        "除湿": "dry",
        "自动": "auto",
    },
}

# Other spellings of the modes (older firmware, other devices) and Korean, which the
# web UI knows but the captured device didn't offer: inferred, merged in below.
_MODE_SYNONYMS: dict[str, dict[str, str]] = {
    "en": {
        "heating": "heat",
        "cooling": "cool",
        "ventilation": "fan",
    },
    "de": {"lüfter": "fan", "ventilator": "fan", "auto": "auto"},
    "es": {
        "calefacción": "heat",
        "refrigeración": "cool",
        "ventilador": "fan",
        "automático": "auto",
    },
    "pt": {
        "aquecimento": "heat",
        "arrefecimento": "cool",
        "refrigeração": "cool",
        "ventilador": "fan",
        "automático": "auto",
    },
    "fr": {
        "chaud": "heat",
        "froid": "cool",
        "refroidissement": "cool",
        "ventilateur": "fan",
        "automatique": "auto",
    },
    "it": {
        "riscaldamento": "heat",
        "raffreddamento": "cool",
        "ventilazione": "fan",
        "ventilatore": "fan",
        "automatico": "auto",
    },
    "ko": {
        "난방": "heat",
        "냉방": "cool",
        "송풍": "fan",
        "제습": "dry",
        "자동": "auto",
    },
}
for _code, _labels in _MODE_SYNONYMS.items():
    MODE_LABELS.setdefault(_code, {}).update(_labels)

# Wall remote lock labels -> allowed / stop_only / forbidden: the settings dialog's
# radios, the list view's lock columns and the values in schedule event texts (which
# use their own words in some languages, e.g. "Erlaubt" next to "Zulässig").
LOCK_LABELS: dict[str, dict[str, str]] = {
    "nl": {
        "toegestaan": "allowed",
        "toegest.": "allowed",
        "alleen stoppen": "stop_only",
        "verboden": "forbidden",
    },
    "en": {
        "permitted": "allowed",
        "allowed": "allowed",
        "stop only": "stop_only",
        "prohibited": "forbidden",
        "forbidden": "forbidden",
    },
    "de": {
        "zulässig": "allowed",
        "erlaubt": "allowed",
        "nur stop": "stop_only",
        "unzulässig": "forbidden",
    },
    "es": {"permitido": "allowed", "sólo paro": "stop_only", "prohibido": "forbidden"},
    "pt": {
        "permitido": "allowed",
        "apenas paragem": "stop_only",
        "proibido": "forbidden",
    },
    "ja": {
        "後押し優先": "allowed",
        "許可": "allowed",
        "停止のみ許可": "stop_only",
        "リモコン禁止": "forbidden",
        "禁止": "forbidden",
    },
    "fr": {"permis": "allowed", "arrêt seul": "stop_only", "interdit": "forbidden"},
    "it": {
        "permesso": "allowed",
        "solo arresto": "stop_only",
        "proibito": "forbidden",
    },
    "zh": {
        "后操作优先": "allowed",
        "允许": "allowed",
        "只允许停止": "stop_only",
        "禁止遥控器": "forbidden",
        "禁止": "forbidden",
    },
}

# The keys of the wall remote lock settings in schedule event texts ("<key>[<value>]")
# -> which lock they are.
EVENT_LOCK_KEYS: dict[str, dict[str, str]] = {
    "nl": {
        "afst.bed.start/stop vergrendelen": "lock_start_stop",
        "afst.bed.mode vergrendelen": "lock_mode",
        "afst.bed.temp vergrendelen": "lock_setpoint",
    },
    "en": {
        "r/c start/stop": "lock_start_stop",
        "r/c op. mode": "lock_mode",
        "r/c setpoint": "lock_setpoint",
    },
    "de": {
        "fb.start/stop": "lock_start_stop",
        "fb modus": "lock_mode",
        "fb sollwert": "lock_setpoint",
    },
    "es": {
        "c/r marcha/paro": "lock_start_stop",
        "c/r modo": "lock_mode",
        "c/r consigna": "lock_setpoint",
    },
    "pt": {
        "r/c on/off": "lock_start_stop",
        "r/c modo func.": "lock_mode",
        "r/c ajuste temp.": "lock_setpoint",
    },
    "ja": {
        "リモコン発停": "lock_start_stop",
        "リモコン運転モード": "lock_mode",
        "リモコン温度設定": "lock_setpoint",
    },
    "fr": {
        "télécommande marche/arrêt": "lock_start_stop",
        "télécommande mode fonctionnement": "lock_mode",
        "télécommande point de consigne": "lock_setpoint",
    },
    "it": {
        "r/c on/off": "lock_start_stop",
        "r/c modo oper.": "lock_mode",
        "r/c setpoint": "lock_setpoint",
    },
    "zh": {
        "复位起动/停止": "lock_start_stop",
        "复位运转模式": "lock_mode",
        "复位温度设定": "lock_setpoint",
    },
}

# Status lines on the Details screen: "<key> [<value>]".
DETAIL_KEYS: dict[str, dict[str, str]] = {
    "nl": {"thermostaatstatus": "thermo", "r32 alarm": "r32"},
    "en": {"thermostat status": "thermo", "r32 alarm": "r32"},
    "de": {"thermostatstatus": "thermo", "r32 alarm": "r32"},
    "es": {"est. termostato": "thermo", "r32 alarma": "r32"},
    "pt": {"estado do termóstato": "thermo", "r32 alarme": "r32"},
    "ja": {"サーモ状態": "thermo", "r32警報": "r32"},
    "fr": {"état du thermostat": "thermo", "alarme r32": "r32"},
    "it": {"stato del termostato": "thermo", "allarme r32": "r32"},
    "zh": {"温控器状态": "thermo", "r32警报": "r32"},
}

# On/off-like values: Details status values, the maintenance column, the overtime
# combo and e-mail activation. The "on" side of the thermostat and R32 values is
# inferred where the device only showed "off".
ON_OFF_LABELS: dict[str, dict[str, bool]] = {
    "nl": {"aan": True, "uit": False, "actief": True, "deactiv.": False},
    "en": {
        "on": True,
        "off": False,
        "active": True,
        "inactive": False,
        "enable": True,
        "disable": False,
    },
    "de": {"an": True, "aus": False, "aktiv.": True, "deaktiv.": False},
    "es": {"on": True, "off": False, "hab.": True, "desh.": False},
    "pt": {
        "on": True,
        "off": False,
        "ligado": True,
        "desligado": False,
        "ativar": True,
        "desativar": False,
    },
    "ja": {"on": True, "off": False, "有効": True, "無効": False},
    "fr": {
        "on": True,
        "off": False,
        "marche": True,
        "arrêt": False,
        "activer": True,
        "désactiver": False,
    },
    "it": {"on": True, "off": False, "attiva": True, "disattiva": False},
    "zh": {"on": True, "off": False, "有效": True, "无效": False},
}

# On/off and setback words -> on / off / setback_low / setback_high: the operation
# panel's combo and the first word of a schedule event text. Setback high in event
# texts is inferred from setback low.
POWER_LABELS: dict[str, dict[str, str]] = {
    "nl": {
        "start": "on",
        "stop": "off",
        "setback laag": "setback_low",
        "setback hoog": "setback_high",
        "sb laag": "setback_low",
        "sb hoog": "setback_high",
    },
    "en": {
        "start": "on",
        "stop": "off",
        "setback low": "setback_low",
        "setback high": "setback_high",
        "sb low": "setback_low",
        "sb high": "setback_high",
    },
    "de": {
        "start": "on",
        "stop": "off",
        "setback niedrig": "setback_low",
        "setback hoch": "setback_high",
        "sb niedrig": "setback_low",
        "sb hoch": "setback_high",
    },
    "es": {
        "marcha": "on",
        "paro": "off",
        "standby bajo": "setback_low",
        "standby alto": "setback_high",
        "rg baja": "setback_low",
        "rg alta": "setback_high",
    },
    "pt": {
        "iniciar": "on",
        "parar": "off",
        "bloqueio baixo": "setback_low",
        "bloqueio alto": "setback_high",
        "a baix": "setback_low",
        "at alto": "setback_high",
    },
    "ja": {
        "運転": "on",
        "停止": "off",
        "セットバック弱": "setback_low",
        "セットバック強": "setback_high",
        "sb弱": "setback_low",
        "sb強": "setback_high",
    },
    "fr": {
        "démarrer": "on",
        "arrêter": "off",
        "setback bas": "setback_low",
        "setback haut": "setback_high",
        "sb bas": "setback_low",
        "sb haut": "setback_high",
    },
    "it": {
        "avvia": "on",
        "arresta": "off",
        "setback basso": "setback_low",
        "setback alto": "setback_high",
        "sb basso": "setback_low",
        "sb alto": "setback_high",
    },
    "zh": {
        "运转": "on",
        "停止": "off",
        "温度维持弱": "setback_low",
        "温度维持强": "setback_high",
        "sb弱": "setback_low",
        "sb强": "setback_high",
    },
}

# The name of the iTM's built-in "All" zone, which it translates with the display
# language.
ALL_ZONE_LABELS: dict[str, str] = {
    "nl": "Alles",
    "en": "All",
    "de": "Alle",  # codespell:ignore alle
    "es": "Todo",
    "pt": "Todos",
    "ja": "全て",
    "fr": "Tout",
    "it": "Tutto",
    "zh": "全部",
}

# Web access user types -> administrator (True) or user (False). The device only had
# administrators; the user types are inferred.
USER_TYPE_LABELS: dict[str, dict[str, bool]] = {
    "nl": {"beheerder": True, "gebruiker": False},
    "en": {"manager": True, "user": False},
    "de": {"administrator": True, "benutzer": False},
    "es": {"gestor": True, "usuario": False},
    "pt": {"gestor": True, "utilizador": False},
    "ja": {"管理者": True, "一般": False},
    "fr": {"administrateur": True, "utilisateur": False},
    "it": {"gestore": True, "utente": False},
    "zh": {"管理员": True, "用户": False},
}

# Attribute lines of an analog point's Details screen: "<key>:<value>". Lower limits
# are inferred (the device's analog points only had an upper limit).
ANALOG_KEYS: dict[str, dict[str, str]] = {
    "nl": {
        "doelpunt": "source_point",
        "d. anal.wrd": "source_value",
        "bovengrens": "upper_limit",
        "ondergrens": "lower_limit",
        "tekenreeks eenheid": "unit",
    },
    "en": {
        "target point": "source_point",
        "target analog value": "source_value",
        "upper limit": "upper_limit",
        "lower limit": "lower_limit",
        "unit label": "unit",
    },
    "de": {
        "ziel-punkt": "source_point",
        "t. analog vl.": "source_value",
        "obergrenze": "upper_limit",
        "untergrenze": "lower_limit",
        "einheit": "unit",
    },
    "es": {
        "punto objetivo": "source_point",
        "v. analógico ob.": "source_value",
        "límite sup.": "upper_limit",
        "límite inf.": "lower_limit",
        "unidad": "unit",
    },
    "pt": {
        "ponto de destino": "source_point",
        "vl. analóg a.": "source_value",
        "limite superior": "upper_limit",  # codespell:ignore limite
        "limite inferior": "lower_limit",  # codespell:ignore limite
        "sequência de unidade": "unit",
    },
    "ja": {
        "対象管理点": "source_point",
        "対象アナログ値": "source_value",
        "上限値": "upper_limit",
        "下限値": "lower_limit",
        "単位文字列": "unit",
    },
    "fr": {
        "point cible": "source_point",
        "vl. analog c.": "source_value",
        "limite haute": "upper_limit",  # codespell:ignore limite
        "limite basse": "lower_limit",  # codespell:ignore limite
        "abréviation d'unité": "unit",
    },
    "it": {
        "punto target": "source_point",
        "vl analog t": "source_value",
        "limite superiore": "upper_limit",  # codespell:ignore limite
        "limite inferiore": "lower_limit",  # codespell:ignore limite
        "stringa unità": "unit",
    },
    "zh": {
        "目標管理点": "source_point",
        "目标模拟值": "source_value",
        "上限值": "upper_limit",
        "下限值": "lower_limit",
        "单位字符串": "unit",
    },
}


def _lookup(tables: dict[str, dict[str, str]], label: str | None) -> str | None:
    if not label:
        return None
    key = label.strip().lower()
    for table in tables.values():
        if key in table:
            return table[key]
    return None


def mode_from_label(label: str | None) -> str | None:
    """Map an operation mode label to heat / cool / fan / dry / auto, or None."""
    return _lookup(MODE_LABELS, label)


def lock_from_label(label: str | None) -> str | None:
    """Map a wall remote lock label to allowed / stop_only / forbidden, or None."""
    return _lookup(LOCK_LABELS, label)


def detail_key(label: str) -> str | None:
    """Map a Details status line key to thermo / r32, or None."""
    return _lookup(DETAIL_KEYS, label)


def power_from_label(label: str | None) -> str | None:
    """Map an on/off label to on / off / setback_low / setback_high, or None."""
    return _lookup(POWER_LABELS, label)


def analog_key(label: str) -> str | None:
    """Map an analog point's Details attribute key to an AnalogPoint field, or None."""
    return _lookup(ANALOG_KEYS, label)


def on_off(label: str) -> bool | None:
    """Map an on/off-like label to a bool, or None when unknown."""
    key = label.strip().lower()
    for table in ON_OFF_LABELS.values():
        if key in table:
            return table[key]
    return None


def user_is_admin(label: str) -> bool | None:
    """Map a web access user type to administrator (True) or user, or None."""
    key = label.strip().lower()
    for table in USER_TYPE_LABELS.values():
        if key in table:
            return table[key]
    return None


def is_all_zone(name: str) -> bool:
    """Tell whether a zone name is the built-in "All" zone, in any language."""
    return name.strip() in ALL_ZONE_LABELS.values()


def event_lock_key(label: str) -> str | None:
    """Map an event text's lock key to lock_start_stop / lock_mode / lock_setpoint."""
    return _lookup(EVENT_LOCK_KEYS, label)
