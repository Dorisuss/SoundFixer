"""Тонкая и быстрая обёртка над Windows Core Audio (MMDevice API).

Все методы вызываются только из потока управления (control.Worker, COM MTA).
В отличие от pycaw.AudioUtilities.GetAllDevices здесь не читается всё хранилище
свойств каждого устройства — только два нужных свойства, это в разы быстрее.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

RENDER, CAPTURE = 0, 1
ROLES = (0, 1, 2)  # eConsole, eMultimedia, eCommunications
STATE_ACTIVE = 0x1
STATE_PRESENT_MASK = 0x1 | 0x2 | 0x8  # active | disabled | unplugged

VIRTUAL_HINTS = ("vb-audio", "cable", "voicemeeter", "virtual", "vac ", "hi-fi cable")


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    adapter: str
    flow: int
    active: bool = True

    @property
    def is_virtual(self) -> bool:
        text = f"{self.name} {self.adapter}".lower()
        return any(h in text for h in VIRTUAL_HINTS)


try:
    import comtypes  # noqa: F401 — инициализирует COM в главном потоке (STA)
    from comtypes import CLSCTX_ALL, CLSCTX_INPROC_SERVER, COMError, GUID, CoCreateInstance
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY
    from pycaw.api.policyconfig import IPolicyConfig
    from pycaw.callbacks import AudioEndpointVolumeCallback, MMNotificationClient
    from pycaw.constants import CLSID_CPolicyConfigClient, CLSID_MMDeviceEnumerator

    AVAILABLE = True
except Exception:  # не Windows или нет зависимостей
    AVAILABLE = False


def _pkey(fmtid: str, pid: int):
    key = PROPERTYKEY()
    key.fmtid = GUID(fmtid)
    key.pid = pid
    return key


if AVAILABLE:
    PKEY_FRIENDLY_NAME = _pkey("{a45c254e-df1c-4efd-8020-67d146a850e0}", 14)
    PKEY_INTERFACE_NAME = _pkey("{026e516e-b814-414b-83cd-856d6fef4822}", 2)

    class _NotificationClient(MMNotificationClient):
        """Колбэки приходят из системных потоков: только передаём событие дальше."""

        def __init__(self, sink):
            super().__init__()
            self._sink = sink

        def on_default_device_changed(self, flow, flow_id, role, role_id, device_id):
            self._sink("default", flow_id, device_id)

        def on_device_added(self, device_id):
            self._sink("topology", None, device_id)

        def on_device_removed(self, device_id):
            self._sink("topology", None, device_id)

        def on_device_state_changed(self, device_id, new_state, new_state_id):
            self._sink("topology", None, device_id)

        def on_property_value_changed(self, *args):
            pass

    class _VolumeWatcher(AudioEndpointVolumeCallback):
        def __init__(self, sink):
            super().__init__()
            self._sink = sink

        def on_notify(self, new_volume, new_mute, event_context, channels, channel_volumes):
            self._sink(float(new_volume), bool(new_mute))


class WinAudio:
    def __init__(self):
        if not AVAILABLE:
            raise RuntimeError("Windows Core Audio недоступен")
        self._enum = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_INPROC_SERVER)
        self._policy = None
        self._notify_client = None
        self._volume_watch = None  # (endpoint_volume, callback)

    # ---------- устройства ----------
    @staticmethod
    def _prop(store, key) -> str:
        try:
            pv = store.GetValue(key)
        except COMError:
            return ""
        try:
            return pv.GetValue() or ""
        finally:
            pv.clear()

    def _device(self, dev, flow: int) -> Device:
        store = dev.OpenPropertyStore(0)  # STGM_READ
        name = self._prop(store, PKEY_FRIENDLY_NAME) or "Неизвестное устройство"
        adapter = self._prop(store, PKEY_INTERFACE_NAME)
        return Device(dev.GetId(), name, adapter, flow, dev.GetState() == STATE_ACTIVE)

    def devices(self, flow: int, active_only: bool = True) -> list[Device]:
        mask = STATE_ACTIVE if active_only else STATE_PRESENT_MASK
        coll = self._enum.EnumAudioEndpoints(flow, mask)
        out = []
        for i in range(coll.GetCount()):
            try:
                out.append(self._device(coll.Item(i), flow))
            except COMError as err:
                log.debug("Пропуск устройства: %s", err)
        return out

    def device(self, device_id: str, flow: int) -> Device | None:
        if not device_id:
            return None
        try:
            return self._device(self._enum.GetDevice(device_id), flow)
        except (COMError, OSError):
            return None

    def default_id(self, flow: int, role: int = 0) -> str | None:
        try:
            return self._enum.GetDefaultAudioEndpoint(flow, role).GetId()
        except (COMError, OSError):
            return None

    def set_default(self, device_id: str, flow: int) -> bool:
        """Делает устройство основным для всех ролей. Возвращает True, если что-то поменяли."""
        changed = False
        for role in ROLES:
            if (self.default_id(flow, role) or "").lower() == device_id.lower():
                continue
            if self._policy is None:
                self._policy = CoCreateInstance(CLSID_CPolicyConfigClient, IPolicyConfig, CLSCTX_ALL)
            try:
                self._policy.SetDefaultEndpoint(device_id, role)
            except (COMError, OSError):
                self._policy = None
                raise
            changed = True
        return changed

    # ---------- громкость устройства (как в микшере Windows) ----------
    def _endpoint_volume(self, device_id: str):
        dev = self._enum.GetDevice(device_id)
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return iface.QueryInterface(IAudioEndpointVolume)

    def get_volume(self, device_id: str) -> float:
        return float(self._endpoint_volume(device_id).GetMasterVolumeLevelScalar())

    def set_volume(self, device_id: str, level: float) -> None:
        self._endpoint_volume(device_id).SetMasterVolumeLevelScalar(max(0.0, min(1.0, level)), None)

    def get_mute(self, device_id: str) -> bool:
        return bool(self._endpoint_volume(device_id).GetMute())

    def set_mute(self, device_id: str, mute: bool) -> None:
        self._endpoint_volume(device_id).SetMute(bool(mute), None)

    # ---------- уведомления ----------
    def watch_devices(self, sink) -> None:
        if self._notify_client is None:
            self._notify_client = _NotificationClient(sink)
            self._enum.RegisterEndpointNotificationCallback(self._notify_client)

    def watch_volume(self, device_id: str | None, sink) -> None:
        """Подписка на изменения громкости одного устройства (None — отписаться)."""
        if self._volume_watch:
            vol, cb = self._volume_watch
            try:
                vol.UnregisterControlChangeNotify(cb)
            except (COMError, OSError):
                pass
            self._volume_watch = None
        if device_id:
            vol = self._endpoint_volume(device_id)
            cb = _VolumeWatcher(sink)
            vol.RegisterControlChangeNotify(cb)
            self._volume_watch = (vol, cb)

    def close(self) -> None:
        self.watch_volume(None, None)
        if self._notify_client is not None:
            try:
                self._enum.UnregisterEndpointNotificationCallback(self._notify_client)
            except (COMError, OSError):
                pass
            self._notify_client = None


def pair_capture(render: Device, captures: list[Device]) -> Device | None:
    """Для playback-стороны виртуального кабеля находит его recording-сторону."""
    if render.adapter:
        for dev in captures:
            if dev.adapter == render.adapter:
                return dev
    # запасной вариант: "CABLE Input (X)" -> "CABLE Output (X)"
    guess = render.name.replace("Input", "Output").replace("input", "output")
    for dev in captures:
        if dev.name == guess:
            return dev
    return None
