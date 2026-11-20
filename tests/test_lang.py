"""The language tables, with the labels a device showed in each display language."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import PowerState, RemoteLock
from daikin_itm.lang import (
    ALL_ZONE_LABELS,
    analog_key,
    calendar_month,
    calendar_week,
    calendar_weekday,
    event_mode_from_label,
    is_all_zone,
    lock_from_label,
    mode_from_label,
    on_off,
    power_from_label,
    user_is_admin,
)
from daikin_itm.menu import ScheduleEvent, event_from_action
from daikin_itm.models import DT_CHARS, analog_details_from_screen, status_from_details
from daikin_itm.protocol import ScreenID
from daikin_itm.state import UIState

# Per language, as captured: the settings dialog's modes (fan, cool, heat), the lock
# radios (allowed, stop only, forbidden), the operation panel's on/off/setback combo,
# schedule event texts and words, the Details status lines, an analog point's
# attributes, the web user types, the thermostat's on and off and the "All" zone.
CAPTURED: dict[str, dict[str, Any]] = {
    "nl": {
        "modes": ["Ventilator", "Koelen", "Verwarmen"],
        "locks": ["Toegestaan", "Alleen stoppen", "Verboden"],
        "power": ["Start", "SB Laag", "SB Hoog", "Stop"],
        "event": "Start, Afst.bed.start/stop vergrendelen[Toegest.], "
        "Afst.bed.mode vergrendelen[Verboden], Afst.bed.temp vergrendelen[Toegest.], "
        "Automatisch, Gemiddeld, 23.0°C",
        "setback": "Setback Laag, 17.0°C",
        "status": "Thermostaatstatus [UIT]\nR32 Alarm [Deactiv.]",
        "analog": "Doelpunt:AC1\nBovengrens:31.0\nTekenreeks eenheid:°C",
        "admin": "Beheerder",
        "off": "UIT",
        "stop": "Stop, Afst.bed.start/stop vergrendelen[Alleen stoppen], "
        "Afst.bed.mode vergrendelen[Verboden], Afst.bed.temp vergrendelen[Verboden]",
        "setback_high": "Setback Hoog",
        "setpoint_mode": "Setpoint",
        "user": "Gebruiker",
        "on": "AAN",
        "all": "Alles",
    },
    "en": {
        "modes": ["Fan", "Cool", "Heat"],
        "locks": ["Permitted", "Stop Only", "Prohibited"],
        "power": ["Start", "SB Low", "SB High", "Stop"],
        "event": "Start, R/C Start/Stop[Permitted], R/C Op. Mode[Prohibited], "
        "R/C Setpoint[Permitted], Automatic, Middle, 23.0°C",
        "setback": "Setback Low, 17.0°C",
        "status": "Thermostat Status [OFF]\nR32 Alarm [Disable]",
        "analog": "Target Point:AC1\nUpper Limit:31.0\nUnit Label:°C",
        "admin": "Manager",
        "off": "Off",
        "stop": "Stop, R/C Start/Stop[Stop Only], R/C Op. Mode[Prohibited], "
        "R/C Setpoint[Prohibited]",
        "setback_high": "Setback High",
        "setpoint_mode": "Dependent",
        "user": "User",
        "on": "ON",
        "all": "All",
    },
    "de": {
        "modes": ["Umluft", "Kühlen ", "Heizen "],
        "locks": ["Zulässig", "Nur Stop", "Unzulässig"],
        "power": ["Start", "SB Niedrig", "SB Hoch", "Stop"],
        "event": "Start, FB.Start/Stop[Erlaubt ], FB Modus[Unzulässig], "
        "FB Sollwert[Erlaubt ], Automatik, Mittel, 23.0°C",
        "setback": "Setback Niedrig, 17.0°C",
        "status": "Thermostatstatus [AUS]\nR32 Alarm [Deaktiv.]",
        "analog": "Ziel-Punkt:AC1\nObergrenze:31.0\nEinheit:°C",
        "admin": "Administrator",
        "off": "AUS",
        "stop": "Stop, FB.Start/Stop[Nur Stop], FB Modus[Unzulässig], "
        "FB Sollwert[Unzulässig]",
        "setback_high": "Setback Hoch",
        "setpoint_mode": "Einstl.pkt",
        "user": "Benutzer",
        "on": "EIN",
        "all": "Alle",  # codespell:ignore alle
    },
    "es": {
        "modes": ["Ventilación", "Frío", "Calor"],
        "locks": ["Permitido", "Sólo paro", "Prohibido"],
        "power": ["Marcha", "RG baja", "RG alta", "Paro"],
        "event": "Marcha, C/R Marcha/Paro[Permitido], C/R Modo[Prohibido], "
        "C/R Consigna[Permitido], Auto, Medio, 23.0°C",
        "setback": "Standby bajo, 17.0°C",
        "status": "Est. termostato [OFF]\nR32 Alarma [Desh.]",
        "analog": "Punto objetivo:AC1\nLímite sup.:31.0\nUnidad:°C",
        "admin": "Gestor",
        "off": "Off",
        "stop": "Paro, C/R Marcha/Paro[Sólo paro], C/R Modo[Prohibido], "
        "C/R Consigna[Prohibido]",
        "setback_high": "Standby alto",
        "setpoint_mode": "Pto.Consigna",
        "user": "Usuario",
        "on": "ON",
        "all": "Todo",
    },
    "pt": {
        "modes": ["Ventilação", "Frio", "Calor"],
        "locks": ["Permitido", "Apenas paragem", "Proibido"],
        "power": ["Iniciar", "A baix", "At alto", "Parar"],
        "event": "Iniciar, R/C On/Off[Permitido], R/C Modo func.[Proibido], "
        "R/C Ajuste temp.[Permitido], Auto, Meio, 23.0°C",
        "setback": "Bloqueio baixo, 17.0°C",
        "status": "Estado do termóstato [DESLIGADO]\nR32 Alarme [Desativar]",
        "analog": "Ponto de destino:AC1\nLimite superior:31.0\nSequência de unidade:°C",
        "admin": "Gestor",
        "off": "OFF",
        "stop": "Parar, R/C On/Off[Apenas paragem], R/C Modo func.[Proibido], "
        "R/C Ajuste temp.[Proibido]",
        "setback_high": "Bloqueio elevado",
        "setpoint_mode": "Temperatura",
        "user": "Utilizador",
        "on": "LIGADO",
        "all": "Todos",
    },
    "ja": {
        "modes": ["送風", "冷房", "暖房"],
        "locks": ["後押し優先", "停止のみ許可", "リモコン禁止"],
        "power": ["運転", "SB弱", "SB強", "停止"],
        "event": "運転, リモコン発停[許可], リモコン運転モード[禁止], "
        "リモコン温度設定[許可], 自動, Middle, 23.0℃",
        "setback": "セットバック弱, 17.0℃",
        "status": "サーモ状態[OFF]\nR32警報[無効]",
        "analog": "対象管理点:AC1\n上限値:31.0\n単位文字列:℃",
        "admin": "管理者",
        "off": "Off",
        "stop": "停止, リモコン発停[停止のみ許可], リモコン運転モード[禁止], "
        "リモコン温度設定[禁止]",
        "setback_high": "セットバック強",
        "setpoint_mode": "温調",
        "user": "一般ユーザ",
        "on": "ON",
        "all": "全て",
    },
    "fr": {
        "modes": ["Ventilation", "Rafraîchir", "Chauffage"],
        "locks": ["Permis", "Arrêt Seul", "Interdit"],
        "power": ["Démarrer", "SB bas", "SB haut", "Arrêter"],
        "event": "Démarrer, Télécommande Marche/Arrêt[Permis], "
        "Télécommande mode fonctionnement[Interdit], "
        "Télécommande point de consigne[Permis], Auto, Moyenne, 23.0°C",
        "setback": "Setback bas, 17.0°C",
        "status": "État du thermostat [ARRÊT]\nAlarme R32 [Désactiver]",
        "analog": "Point cible:AC1\nLimite haute:31.0\nAbréviation d'unité:°C",
        "admin": "Administrateur",
        "off": "OFF",
        "stop": "Arrêter, Télécommande Marche/Arrêt[Arrêt Seul], "
        "Télécommande mode fonctionnement[Interdit], "
        "Télécommande point de consigne[Interdit]",
        "setback_high": "Setback haut",
        "setpoint_mode": "Temp",
        "user": "Utilisateur",
        "on": "MARCHE",
        "all": "Tout",
    },
    "it": {
        "modes": ["Vent", "Freddo", "Caldo"],
        "locks": ["Permesso", "Solo Arresto", "Proibito"],
        "power": ["Avvia", "SB basso", "SB alto", "Arresta"],
        "event": "Avvia, R/C On/Off[Permesso], R/C Modo Oper.[Proibito], "
        "R/C Setpoint[Permesso], Auto, Media, 23.0°C",
        "setback": "Setback basso, 17.0°C",
        "status": "Stato del termostato [OFF]\nAllarme R32 [Disattiva]",
        "analog": "Punto target:AC1\nLimite superiore:31.0\nStringa unità:°C",
        "admin": "Gestore",
        "off": "OFF",
        "stop": "Arresta, R/C On/Off[Solo Arresto], R/C Modo Oper.[Proibito], "
        "R/C Setpoint[Proibito]",
        "setback_high": "Setback alto",
        "setpoint_mode": "Setpoint",
        "user": "Utente",
        "on": "ON",
        "all": "Tutto",
    },
    "zh": {
        "modes": ["通风", "制冷", "制热"],
        "locks": ["后操作优先", "只允许停止", "禁止遥控器"],
        "power": ["运转", "SB弱", "SB强", "停止"],
        "event": "运转, 复位起动/停止[允许], 复位运转模式[禁止], 复位温度设定[允许], "
        "自动, 中, 23.0℃",
        "setback": "温度维持弱, 17.0℃",
        "status": "温控器状态[OFF]\nR32警报[无效]",
        "analog": "目標管理点:AC1\n上限值:31.0\n单位字符串:℃",
        "admin": "管理员",
        "off": "OFF",
        "stop": "停止, 复位起动/停止[只允许停止], 复位运转模式[禁止], "
        "复位温度设定[禁止]",
        "setback_high": "温度维持强",
        "setpoint_mode": "温度控制",
        "user": "用户",
        "on": "ON",
        "all": "全部",
    },
    "ko": {
        "modes": ["송풍", "냉방", "난방"],
        "locks": ["허가", "정지만 허가", "금지"],
        "power": ["운전", "SB저", "SB고", "정지"],
        "event": "운전, 원격 운전/정지[허가], 원격 운전 모드[금지], "
        "원격 온도 설정[허가], 자동, 23.0°C",
        "setback": "세트백 저, 17.0°C",
        "status": "서모 상태 [OFF]\nR32 Alarm [Disable]",
        "analog": "대상 관리점:AC1\n상한치:31.0\n단위 문자 예:°C",
        "admin": "관리자",
        "off": "OFF",
        "stop": "정지, 원격 운전/정지[정지만 허가], 원격 운전 모드[금지], "
        "원격 온도 설정[금지]",
        "setback_high": "세트백 고",
        "setpoint_mode": "온도조절점",
        "user": "사용자",
        "on": "ON",
        "all": "모두",
    },
}
LANGUAGES = list(CAPTURED)


def _details(text: str) -> UIState:
    state = UIState()
    state.screens[ScreenID.DETAILS] = {(DT_CHARS, "TEXT"): text}
    return state


@pytest.mark.parametrize("code", LANGUAGES)
def test_modes_locks_and_power(code: str) -> None:
    """The settings dialog's modes and locks and the panel's on/off combo."""
    c = CAPTURED[code]
    assert [mode_from_label(m) for m in c["modes"]] == ["fan", "cool", "heat"]
    assert [lock_from_label(lock) for lock in c["locks"]] == [
        "allowed",
        "stop_only",
        "forbidden",
    ]
    assert [power_from_label(p) for p in c["power"]] == [
        "on",
        "setback_low",
        "setback_high",
        "off",
    ]
    assert user_is_admin(c["admin"]) is True
    assert user_is_admin(c["user"]) is False
    assert on_off(c["off"]) is False
    assert on_off(c["on"]) is True


@pytest.mark.parametrize("code", LANGUAGES)
def test_event_texts(code: str) -> None:
    """A schedule event text: power, locks, auto mode and the setpoint (°C or ℃)."""
    c = CAPTURED[code]
    event = event_from_action(
        ScheduleEvent(
            time="06:00", target="Zone", target_kind="zone", action=c["event"]
        )
    )
    assert event.power is PowerState.ON
    assert event.mode == "auto"
    assert event.setpoint == 23.0
    assert (event.lock_start_stop, event.lock_mode, event.lock_setpoint) == (
        RemoteLock.ALLOWED,
        RemoteLock.FORBIDDEN,
        RemoteLock.ALLOWED,
    )
    setback = event_from_action(
        ScheduleEvent(
            time="18:00", target="Zone", target_kind="zone", action=c["setback"]
        )
    )
    assert setback.power is PowerState.SETBACK_LOW
    assert setback.setpoint == 17.0


@pytest.mark.parametrize("code", LANGUAGES)
def test_event_stop_setback_high_and_setpoint_mode(code: str) -> None:
    """Stop with stop-only and forbidden locks, setback high, the setpoint-only mode."""
    c = CAPTURED[code]

    def parse(action: str) -> ScheduleEvent:
        return event_from_action(
            ScheduleEvent(
                time="06:15", target="Zone", target_kind="zone", action=action
            )
        )

    stop = parse(c["stop"])
    assert (stop.power, stop.lock_start_stop, stop.lock_mode, stop.lock_setpoint) == (
        PowerState.OFF,
        RemoteLock.STOP_ONLY,
        RemoteLock.FORBIDDEN,
        RemoteLock.FORBIDDEN,
    )
    assert stop.extra == {}
    assert parse(c["setback_high"]).power is PowerState.SETBACK_HIGH
    setpoint = parse(f"{c['setpoint_mode']}, 21.0°C")
    assert (setpoint.mode, setpoint.mode_label, setpoint.setpoint) == (
        "setpoint",
        c["setpoint_mode"],
        21.0,
    )
    assert event_mode_from_label(c["setpoint_mode"]) == "setpoint"


@pytest.mark.parametrize("code", LANGUAGES)
def test_details_and_analog(code: str) -> None:
    """The Details status lines and an analog point's attributes."""
    c = CAPTURED[code]
    status = status_from_details(_details(c["status"]))
    assert status["thermo_on"] is False
    assert status["r32_alarm"] is False
    analog = analog_details_from_screen(_details(c["analog"]))
    assert analog["source_point"] == "AC1"
    assert analog["upper_limit"] == 31.0
    assert analog["unit"] in {"°C", "℃"}


@pytest.mark.parametrize("code", LANGUAGES)
def test_all_zone_in_every_language(code: str) -> None:
    """The built-in "All" zone is recognised in each language; other names aren't."""
    assert is_all_zone(CAPTURED[code]["all"])
    assert set(ALL_ZONE_LABELS) == set(LANGUAGES)


def test_other_zone_names_and_analog_keys() -> None:
    """Other zone names aren't the "All" zone; analog keys outside the samples."""
    assert not is_all_zone("Workroom")
    assert analog_key("Unit Label") == "unit"
    assert analog_key("목표 아날로그 값") == "source_value"


@pytest.mark.parametrize("code", LANGUAGES)
def test_event_lock_by_its_key(code: str) -> None:
    """A single lock is filed by its key, not by its position in the text."""
    action = CAPTURED[code]["event"]
    pairs = [t.strip() for t in action.split(",") if "[" in t]
    mode_lock_only = ", ".join([action.split(",")[0], pairs[1]])
    event = event_from_action(
        ScheduleEvent(
            time="06:00", target="Zone", target_kind="zone", action=mode_lock_only
        )
    )
    assert event.lock_mode is RemoteLock.FORBIDDEN
    assert event.lock_start_stop is None
    assert event.lock_setpoint is None


def test_event_lock_with_unknown_key_falls_back_to_position() -> None:
    """A lock value under a key the tables don't know fills the next free lock."""
    event = event_from_action(
        ScheduleEvent(
            time="06:00",
            target="Zone",
            target_kind="zone",
            action="Start, R/C Start/Stop[Permitted], Unknown key[Prohibited]",
        )
    )
    assert event.lock_start_stop is RemoteLock.ALLOWED
    assert event.lock_mode is RemoteLock.FORBIDDEN


def test_calendar_words() -> None:
    """A calendar's month, week and weekday words, short and long, any case."""
    assert calendar_month("Jan") == 1
    assert calendar_month(" december ") == 12
    assert calendar_month("Every") == 0
    assert calendar_week("1st") == 1
    assert calendar_week("Last") == 5
    assert calendar_weekday("Sun") == 0
    assert calendar_weekday("Saturday") == 6
    assert calendar_month("Janvier") is None
    assert calendar_week("5th") is None
    assert calendar_weekday("") is None
