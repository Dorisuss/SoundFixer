"""Оркестрация: GUI-поток (tkinter), поток управления (COM + аудио), трей, горячие клавиши.

Правила потоков:
  * self.cfg меняется только в GUI-потоке; в поток управления уходят снимки (copy).
  * всё, что трогает COM или открывает аудиопотоки, — только в self.worker.
  * из любых потоков в GUI — только через self.ui(fn, *args).
"""

from __future__ import annotations

import copy
import logging
import os
import sys
import tkinter as tk

from . import APP_NAME, autostart, config, winaudio
from .control import Worker
from .hotkeys import Hotkeys, PushToTalk
from .tray import Tray
from .winaudio import CAPTURE, RENDER, Device, pair_capture

log = logging.getLogger(__name__)


class App:
    def __init__(self, start_hidden: bool):
        self.first_run = not config.CONFIG_FILE.exists()
        self.cfg = config.load()
        self.start_hidden = start_hidden
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self._save_job = None
        self._alive = True

        # состояние, публикуемое потоком управления
        self.inputs: list[Device] = []
        self.outputs: list[Device] = []
        self.defaults: dict[int, str | None] = {RENDER: None, CAPTURE: None}
        self.status = {"mic": "", "spk": "", "devices": ""}
        self.muted = False
        self.ptt_down = False
        self.autostart_enabled = autostart.is_enabled()

        # ресурсы потока управления (создаются и используются только там)
        self.audio: winaudio.WinAudio | None = None
        self.engine = None
        self.tts = None
        self.sounds = None
        self._running_cfg = {"mic": None, "spk": None}
        self._lock_target: tuple[str, float] | None = None
        self._init_tries = 0
        self._engine_retry = 0
        self._active_ids: frozenset = frozenset()
        self._snap = copy.deepcopy(self.cfg)  # снимок настроек для потока управления

        self.window = None  # главное окно создаётся лениво при первом показе
        self.worker = Worker()
        self.tray = Tray(self)
        self.hotkeys = Hotkeys(lambda name: self.ui(self._on_hotkey, name))
        self.ptt = PushToTalk(lambda down: self.ui(self._on_ptt, down))

    # ================================================================ запуск/выход
    def run(self) -> None:
        autostart.repair()
        self.worker.start()
        # init — после старта mainloop, чтобы ответы потока управления сразу доходили до GUI
        self.root.after(0, lambda: self.worker.submit(self._w_init))
        self.tray.start()
        self.hotkeys.start()
        self._sync_hotkeys()
        self._sync_ptt()
        self.root.after(200, self.tray.refresh)
        if not self.start_hidden:
            self.root.after(0, self.show_window)
        self.root.mainloop()

    def quit(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self.save_now()
        self.ptt.stop()
        self.hotkeys.stop()
        try:
            self.worker.call(self._w_shutdown, timeout=5)
        except Exception:  # noqa: BLE001
            log.exception("Остановка")
        self.worker.stop()
        self.tray.stop()
        self.root.after(50, self.root.destroy)

    def ui(self, fn, *args) -> None:
        """Выполнить fn в GUI-потоке (безопасно вызывать из любого потока)."""
        if not self._alive and fn is not self.quit:
            return
        try:
            self.root.after(0, fn, *args)
        except (RuntimeError, tk.TclError):
            pass

    # ================================================================ конфиг
    def save_soon(self) -> None:
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(800, self.save_now)

    def save_now(self) -> None:
        self._save_job = None
        try:
            config.save(self.cfg)
        except OSError as err:
            log.error("Не удалось сохранить настройки: %s", err)

    @property
    def profile(self) -> dict:
        return self.cfg["profiles"][self.cfg["active_profile"]]

    def notify(self, title: str, text: str) -> None:
        if self.cfg.get("notifications", True):
            self.tray.notify(title, text)

    # ================================================================ окно
    def show_window(self) -> None:
        if self.window is None:
            from .gui import MainWindow

            self.window = MainWindow(self)
        self.window.show()

    def refresh_ui(self) -> None:
        self.tray.refresh()
        if self.window is not None:
            self.window.refresh()

    # ================================================================ действия (GUI-поток)
    def apply_now(self) -> None:
        snap = self._publish()
        self.worker.submit(lambda: self._w_sync(snap, force=True), key="sync")

    def switch_profile(self, name: str) -> None:
        if name not in self.cfg["profiles"]:
            return
        self.cfg["active_profile"] = name
        self.save_soon()
        self.settings_changed(devices=True)
        self.notify(APP_NAME, f"Профиль: {name}")

    def set_device(self, kind: str, device_id: str | None) -> None:
        self.profile["input_id" if kind == "in" else "output_id"] = device_id
        self.save_soon()
        self.settings_changed(devices=True)

    def toggle_feature(self, section: str) -> None:
        self.cfg[section]["enabled"] = not self.cfg[section]["enabled"]
        self.save_soon()
        self.settings_changed(devices=True)

    def toggle_autostart(self) -> None:
        try:
            autostart.set_enabled(not self.autostart_enabled)
        except OSError as err:
            log.error("Автозапуск: %s", err)
        self.autostart_enabled = autostart.is_enabled()
        self.refresh_ui()

    def settings_changed(self, devices: bool = False, hotkeys: bool = False, sync: bool = False) -> None:
        """Вызывается GUI при любом изменении настроек.

        devices — сразу перепроверить устройства/аудиопотоки; sync — перепроверить с задержкой
        (схлопывается при перетаскивании ползунка). Параметры эффектов применяются мгновенно сами.
        """
        self.save_soon()
        eng = self.engine
        if eng is not None:  # параметры звука применяются мгновенно, без потока управления
            self._push_dsp(eng)
        if hotkeys:
            self._sync_hotkeys()
            self._sync_ptt()
        snap = self._publish()
        if devices:
            self.worker.submit(lambda: self._w_sync(snap), key="sync")
        elif sync:
            self.worker.submit(lambda: self._w_sync(snap), delay=0.3, key="sync")
        self.tray.refresh()

    def _push_dsp(self, eng) -> None:
        mic, spk = self.cfg["mic"], self.cfg["spk"]
        eng.mic_chain.configure(mic)
        eng.spk_chain.configure(spk)
        eng.monitor_voice = bool(mic["monitor"])
        eng.monitor_gain = mic["monitor_volume"] / 100.0
        eng.muted = self.muted

    def _publish(self) -> dict:
        """GUI-поток: обновить снимок настроек, который читает поток управления."""
        self._snap = copy.deepcopy(self.cfg)
        return self._snap

    # ---------------------------------------------------------------- микрофон: mute / PTT
    def toggle_mute(self) -> None:
        self.set_muted(not self.muted, announce=True)

    def set_muted(self, muted: bool, announce: bool = False) -> None:
        if muted == self.muted:
            return
        self.muted = muted
        eng_on = self.engine is not None and self.engine.mic_running
        if self.engine is not None:
            self.engine.muted = muted
        # без обработки — выключаем сам микрофон в Windows; с обработкой он должен быть включён
        dev = self.profile["input_id"] or self.defaults.get(CAPTURE)
        if dev:
            hw_mute = muted and not eng_on
            self.worker.submit(lambda: self.audio and self.audio.set_mute(dev, hw_mute))
        if announce:
            self.notify(APP_NAME, "Микрофон выключен" if muted else "Микрофон включён")
        self.refresh_ui()

    def _on_ptt(self, down: bool) -> None:
        self.ptt_down = down
        self.set_muted(not down)

    def _sync_ptt(self) -> None:
        mic = self.cfg["mic"]
        if mic["ptt"]:
            if self.ptt.start(mic["ptt_key"]):
                self.set_muted(True)
        elif self.ptt.running:
            self.ptt.stop()
            self.set_muted(False)

    def _sync_hotkeys(self) -> None:
        b = {"mute": self.cfg["mic"]["mute_hotkey"], "stop": self.cfg["soundboard"]["stop_hotkey"]}
        for i, snd in enumerate(self.cfg["soundboard"]["sounds"]):
            if snd.get("hotkey"):
                b[f"sound:{i}"] = snd["hotkey"]
        self.hotkeys.set_bindings(b)

    def _on_hotkey(self, name: str) -> None:
        if name == "mute":
            self.toggle_mute()
        elif name == "stop":
            self.stop_sounds()
        elif name.startswith("sound:"):
            self.play_sound(int(name.split(":", 1)[1]))

    # ---------------------------------------------------------------- озвучка / саундборд
    def _mic_ready(self) -> bool:
        if self.engine is not None and self.engine.mic_running:
            return True
        self.report("mic", "Сначала включите «Обработку микрофона» — озвучка и звуки идут через неё.")
        return False

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text or not self._mic_ready():
            return
        t = copy.deepcopy(self.cfg["tts"])
        hist = [h for h in t["history"] if h != text]
        self.cfg["tts"]["history"] = ([text] + hist)[:20]
        self.save_soon()

        def job():
            from .media import TextToSpeech

            if self.tts is None:
                self.tts = TextToSpeech()
            data = self.tts.synth(text, t["voice"], t["rate"], t["volume"])
            if self.engine is not None:
                self.engine.mixer.play(data, 1.0, t["through_fx"], t["monitor"])

        self._submit_reporting(job, "mic", "Озвучка")

    def list_voices(self, callback) -> None:
        def job():
            from .media import TextToSpeech

            if self.tts is None:
                self.tts = TextToSpeech()
            voices = self.tts.voices()
            self.ui(callback, voices)

        self._submit_reporting(job, "mic", "Голоса озвучки")

    def play_sound(self, index: int) -> None:
        sounds = self.cfg["soundboard"]["sounds"]
        if not (0 <= index < len(sounds)) or not self._mic_ready():
            return
        path = sounds[index]["path"]
        sb = self.cfg["soundboard"]
        vol, mon = sb["volume"] / 100.0, sb["monitor"]

        def job():
            from .media import SoundCache

            if self.sounds is None:
                self.sounds = SoundCache()
            data = self.sounds.get(path)
            if self.engine is not None:
                self.engine.mixer.play(data, vol, False, mon)

        self._submit_reporting(job, "mic", os.path.basename(path))

    def stop_sounds(self) -> None:
        if self.engine is not None:
            self.engine.mixer.stop()

    def _submit_reporting(self, job, section: str, what: str) -> None:
        def wrapped():
            try:
                job()
            except Exception as err:  # noqa: BLE001
                log.exception(what)
                self.ui(self.report, section, f"{what}: ошибка — {err}")

        self.worker.submit(wrapped)

    def report(self, section: str, text: str) -> None:
        self.status[section] = text
        if self.window is not None:
            self.window.refresh_status()

    # ================================================================ поток управления
    def _w_init(self) -> None:
        try:
            self.audio = winaudio.WinAudio()
            self.audio.watch_devices(self._on_device_event)
        except Exception as err:  # noqa: BLE001 — служба звука ещё не готова (ранний автозапуск)
            self.audio = None
            self._init_tries += 1
            log.warning("Core Audio недоступен (%s), попытка %d", err, self._init_tries)
            if self._init_tries <= 20:
                self.worker.submit(self._w_init, delay=3.0)
            else:
                self.ui(self.report, "devices", f"Звуковая служба Windows недоступна: {err}")
            return
        self._active_ids = self._w_active_ids()
        self._w_sync(self._snap, force=True)

    def _w_active_ids(self) -> frozenset:
        a = self.audio
        return frozenset(d.id for d in a.devices(CAPTURE) + a.devices(RENDER))

    def _w_shutdown(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        if self.audio is not None:
            snap = copy.deepcopy(self._snap)
            snap["mic"]["enabled"] = snap["spk"]["enabled"] = False
            try:  # вернуть настоящие устройства, чтобы звук не «пропал» после выхода
                self._w_reconcile(snap, force=True)
            except Exception:  # noqa: BLE001
                pass
            self.audio.close()

    def _on_device_event(self, kind: str, flow, device_id) -> None:
        """Из системного потока Windows: только ставим задачу (с debounce)."""
        if kind == "default":
            self.worker.submit(self._w_on_default_changed, delay=0.4, key="default")
        else:
            self.worker.submit(self._w_on_topology, delay=1.0, key="topology")

    def _w_on_default_changed(self) -> None:
        if self.audio is None:
            return
        snap = self._snap
        if snap["enforce_defaults"]:
            self._w_reconcile(snap)
        else:
            self._w_publish_devices()

    def _w_on_topology(self) -> None:
        if self.audio is None:
            return
        snap = self._snap
        ids = self._w_active_ids()
        topology_changed = ids != self._active_ids
        self._active_ids = ids
        if topology_changed and self.engine is not None and (self.engine.mic_running or self.engine.spk_running):
            self.engine.stop_mic()
            self.engine.stop_spk()
            self._running_cfg = {"mic": None, "spk": None}
            self.engine.rescan()
        self._w_sync(snap, force=snap["enforce_defaults"])

    def _w_publish_devices(self) -> None:
        a = self.audio
        ins, outs = a.devices(CAPTURE), a.devices(RENDER)
        defaults = {RENDER: a.default_id(RENDER), CAPTURE: a.default_id(CAPTURE)}
        self.ui(self._set_devices, ins, outs, defaults)

    def _set_devices(self, ins, outs, defaults) -> None:
        self.inputs, self.outputs, self.defaults = ins, outs, defaults
        if self.first_run:  # первый запуск: профиль = текущие устройства Windows
            self.first_run = False
            prof = self.profile
            by_id = {d.id: d for d in ins + outs}
            for key, flow in (("input_id", CAPTURE), ("output_id", RENDER)):
                dev = by_id.get(defaults.get(flow) or "")
                if not prof[key] and dev is not None and not dev.is_virtual:
                    prof[key] = dev.id
            self._publish()
            self.save_soon()
        if self.window is not None:
            self.window.refresh_devices()

    def _w_sync(self, snap: dict, force: bool = False) -> None:
        """Главная проверка: аудиодвижок + устройства по умолчанию + фиксация уровня."""
        if self.audio is None:
            return
        self._w_engine(snap)
        self._w_reconcile(snap, force=force)

    # ---------------------------------------------------------------- устройства
    def _virtual_pair(self, render_name: str, outs, ins):
        render = next((d for d in outs if d.name == render_name), None)
        if render is None:
            return None, None
        return render, pair_capture(render, ins)

    def _w_reconcile(self, snap: dict, force: bool = False) -> None:
        a = self.audio
        if a is None:
            return
        ins, outs = a.devices(CAPTURE), a.devices(RENDER)
        prof = snap["profiles"][snap["active_profile"]]
        eng = self.engine

        want_out = prof["output_id"]
        if snap["spk"]["enabled"] and eng is not None and eng.spk_running:
            render, _ = self._virtual_pair(snap["virtual_spk"], outs, ins)
            if render:
                want_out = render.id
        want_in = prof["input_id"]
        if snap["mic"]["enabled"] and snap["mic"]["set_virtual_default"] and eng is not None \
                and eng.mic_running and snap["virtual_mic"]:
            _, capture = self._virtual_pair(snap["virtual_mic"], outs, ins)
            if capture:
                want_in = capture.id

        changed = []
        if force or snap["enforce_defaults"]:
            active = {d.id.lower() for d in ins + outs}
            for dev_id, flow in ((want_out, RENDER), (want_in, CAPTURE)):
                if dev_id and dev_id.lower() in active:
                    try:
                        if a.set_default(dev_id, flow):
                            changed.append(flow)
                    except Exception as err:  # noqa: BLE001
                        log.error("Не удалось назначить устройство: %s", err)
                        self.ui(self.report, "devices", f"Ошибка назначения устройства: {err}")
        if changed:
            log.info("Восстановлены устройства по умолчанию: %s", changed)

        # фиксация громкости микрофона (на том устройстве, которое видят программы)
        mic = snap["mic"]
        target = (want_in, mic["lock_level_value"] / 100.0) if mic["lock_level"] and want_in else None
        if target != self._lock_target:
            self._lock_target = target
            try:
                if target:
                    a.set_volume(target[0], target[1])
                    a.watch_volume(target[0], self._on_volume_event)
                else:
                    a.watch_volume(None, None)
            except Exception as err:  # noqa: BLE001
                log.warning("Фиксация уровня микрофона: %s", err)

        defaults = {RENDER: a.default_id(RENDER), CAPTURE: a.default_id(CAPTURE)}
        self.ui(self._set_devices, ins, outs, defaults)

    def _on_volume_event(self, level: float, _muted: bool) -> None:
        target = self._lock_target
        if target and abs(level - target[1]) > 0.004:
            self.worker.submit(self._w_restore_level, delay=0.05, key="level")

    def _w_restore_level(self) -> None:
        target = self._lock_target
        if target and self.audio:
            try:
                self.audio.set_volume(target[0], target[1])
            except Exception as err:  # noqa: BLE001
                log.warning("Не удалось вернуть уровень микрофона: %s", err)

    # ---------------------------------------------------------------- аудиодвижок
    def _w_engine(self, snap: dict) -> None:
        mic_on, spk_on = snap["mic"]["enabled"], snap["spk"]["enabled"]
        eng = self.engine
        if not (mic_on or spk_on):
            if eng is not None and (eng.mic_running or eng.spk_running):
                eng.stop()
            self._running_cfg = {"mic": None, "spk": None}
            self.ui(self._set_engine_status, "", "")
            return

        a = self.audio
        ins, outs = a.devices(CAPTURE), a.devices(RENDER)
        by_id = {d.id.lower(): d for d in ins + outs}
        prof = snap["profiles"][snap["active_profile"]]
        mic_dev = by_id.get((prof["input_id"] or "").lower())
        out_dev = by_id.get((prof["output_id"] or "").lower())
        if out_dev is None:  # наушники не выбраны — берём текущее устройство Windows (не виртуальное)
            out_dev = next((d for d in outs if not d.is_virtual), None)
        if mic_dev is None:
            mic_dev = next((d for d in ins if not d.is_virtual), None)

        mic_status = spk_status = ""
        want_mic = want_spk = None
        mic_cable_capture = ""
        if mic_on:
            cable, cable_capture = self._virtual_pair(snap["virtual_mic"], outs, ins) if snap["virtual_mic"] \
                else (None, None)
            monitor_needed = snap["mic"]["monitor"] or snap["tts"]["monitor"] or snap["soundboard"]["monitor"]
            if mic_dev is None:
                mic_status = "Микрофон не найден"
            elif mic_dev.is_virtual:
                mic_status = "В профиле выбран виртуальный микрофон — выберите настоящий"
            else:
                want_mic = (mic_dev.name, cable.name if cable else None,
                            out_dev.name if (out_dev and monitor_needed) else None)
                mic_cable_capture = cable_capture.name if cable_capture else (cable.name if cable else "")
                if cable is None:
                    mic_status = ("Виртуальный кабель не выбран: эффекты слышите только вы. "
                                  "Установите VB-Cable и выберите его во вкладке «Настройки».")
        if spk_on:
            render, capture = self._virtual_pair(snap["virtual_spk"], outs, ins) if snap["virtual_spk"] \
                else (None, None)
            if not snap["virtual_spk"] or render is None or capture is None:
                spk_status = "Выберите виртуальный кабель для звука системы во вкладке «Настройки»"
            elif snap["virtual_spk"] == snap["virtual_mic"] and mic_on:
                spk_status = "Для микрофона и наушников нужны два разных виртуальных кабеля"
            elif out_dev is None or out_dev.is_virtual:
                spk_status = "Выберите настоящие наушники/динамики в профиле"
            else:
                want_spk = (capture.name, out_dev.name)

        try:
            if eng is None and (want_mic or want_spk):
                from .engine import Engine  # ленивая загрузка numpy/pedalboard/PortAudio

                eng = self.engine = Engine(snap["block_size"])
            if eng is not None:
                self._push_dsp_snapshot(eng, snap)
                if eng.block != snap["block_size"]:
                    eng.stop()
                    eng.block = snap["block_size"]
                    self._running_cfg = {"mic": None, "spk": None}
                if not eng.healthy():
                    eng.stop_mic()
                    eng.stop_spk()
                    self._running_cfg = {"mic": None, "spk": None}
                    eng.rescan()
                if want_mic != self._running_cfg["mic"]:
                    eng.stop_mic()
                    self._running_cfg["mic"] = None
                    if want_mic:
                        try:
                            eng.start_mic(*want_mic)
                            self._running_cfg["mic"] = want_mic
                        except Exception as err:  # noqa: BLE001
                            mic_status = str(err)
                if want_spk != self._running_cfg["spk"]:
                    eng.stop_spk()
                    self._running_cfg["spk"] = None
                    if want_spk:
                        try:
                            eng.start_spk(*want_spk)
                            self._running_cfg["spk"] = want_spk
                        except Exception as err:  # noqa: BLE001
                            spk_status = str(err)
        except Exception as err:  # noqa: BLE001 — например, не установлены зависимости
            log.exception("Аудиодвижок")
            mic_status = spk_status = f"Аудиодвижок недоступен: {err}"

        failed = (want_mic and not self._running_cfg["mic"]) or (want_spk and not self._running_cfg["spk"])
        if failed and self._engine_retry < 6:  # устройство могло ещё не проснуться (автозапуск)
            self._engine_retry += 1
            self.worker.submit(lambda: self._w_sync(self._snap), delay=5.0 * self._engine_retry, key="sync")
        elif not failed:
            self._engine_retry = 0
        if eng is not None and eng.mic_running:
            eng.muted = self.muted
            if not mic_status:
                mic_status = "Работает" + (f" — программы слышат вас через «{mic_cable_capture}»"
                                           if mic_cable_capture else "")
        if eng is not None and eng.spk_running and not spk_status:
            spk_status = f"Работает — звук идёт в: {want_spk[1]}"
        self.ui(self._set_engine_status, mic_status, spk_status)

    def _push_dsp_snapshot(self, eng, snap) -> None:
        eng.mic_chain.configure(snap["mic"])
        eng.spk_chain.configure(snap["spk"])
        eng.monitor_voice = bool(snap["mic"]["monitor"])
        eng.monitor_gain = snap["mic"]["monitor_volume"] / 100.0

    def _set_engine_status(self, mic: str, spk: str) -> None:
        self.status["mic"], self.status["spk"] = mic, spk
        self.refresh_ui()


def setup_logging() -> None:
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = []
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(config.LOG_FILE, maxBytes=256_000, backupCount=1, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:  # pythonw: stderr нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def excepthook(exc_type, exc, tb):
        logging.getLogger("crash").critical("Необработанная ошибка", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    setup_logging()
    if sys.platform == "win32":
        import ctypes

        try:  # чёткий интерфейс на мониторах с масштабированием
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SoundFixer.App")

    from .single import SingleInstance

    inst = SingleInstance()
    if not inst.primary:
        inst.notify_primary()
        return 0
    app = App(start_hidden=autostart.TRAY_ARG in argv)
    inst.listen(lambda: app.ui(app.show_window))
    app.run()
    return 0
