"""Оркестрация: GUI-поток (tkinter), поток управления (COM + аудио), поток озвучки, трей, клавиши.

Правила потоков:
  * self.cfg меняется только в GUI-потоке; другим потокам доступен снимок self._snap.
  * всё, что трогает Core Audio или открывает/закрывает аудиопотоки, — только в self.worker.
  * синтез речи и декодирование звуков — в self.media (не тормозит переключение устройств).
  * из любых потоков в GUI — только через self.ui(fn, *args).
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import logging
import math
import os
import sys
import threading
import time
import tkinter as tk

from . import APP_NAME, autostart, config, winaudio
from .control import Worker
from .hotkeys import Hotkeys, PushToTalk
from .tools import ToolManager
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

        # состояние, публикуемое другими потоками (читается GUI)
        self.inputs: list[Device] = []
        self.outputs: list[Device] = []
        self.defaults: dict[int, str | None] = {RENDER: None, CAPTURE: None}
        self.status = {"mic": "", "spk": "", "devices": "", "speech": "", "denoise": "", "tips": ""}
        self.status_ok: dict[str, bool | None] = {}
        self.muted = False
        self.ptt_down = False
        self.autostart_enabled = autostart.is_enabled()

        # ресурсы потока управления
        self.audio: winaudio.WinAudio | None = None
        self.engine = None
        self._running_cfg = {"mic": None, "spk": None}
        self._lock_target: tuple[str, float] | None = None
        self._dn_engine = None          # какая нейросеть шумодава установлена в цепочку
        self._overload = 0
        self.denoise_info: tuple[str, int] | None = None  # (название, задержка мс) — для окна
        self._init_tries = 0
        self._engine_retry = 0
        self._active_ids: frozenset = frozenset()
        self._snap = copy.deepcopy(self.cfg)
        # ресурсы потока озвучки
        self._tts = None
        self._sounds = None

        self.window = None  # главное окно создаётся лениво при первом показе
        self.worker = Worker()
        self.media = Worker("SoundFixer-media")
        self.tray = Tray(self)
        self.hotkeys = Hotkeys(lambda name: self.ui(self._on_hotkey, name))
        self.ptt = PushToTalk(lambda down: self.ui(self._on_ptt, down))
        self.tools = ToolManager(self)
        self._osd = None

    # ================================================================ запуск/выход
    def run(self) -> None:
        autostart.repair()
        self.worker.start()
        self.media.start()
        # init — после старта mainloop, чтобы ответы потока управления сразу доходили до GUI
        self.root.after(0, lambda: self.worker.submit(self._w_init))
        self.tray.start()
        self.hotkeys.start()
        self.tools.sync(self.cfg["tools"])
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
        self.tools.stop_all()  # вернуть настройки Windows (залипание клавиш, сон, гамму) как были
        if self._osd is not None:
            self._osd.destroy()
        try:
            self.worker.call(self._w_shutdown, timeout=5)
        except Exception:  # noqa: BLE001
            log.exception("Остановка")
        self.worker.stop()
        self.media.stop()
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

    @property
    def osd(self):
        """Подсказка поверх всех окон (не сворачивает игры). Только GUI-поток; None вне Windows."""
        if self._osd is None and sys.platform == "win32":
            from .tools.overlay import Osd

            self._osd = Osd(self.root)
        return self._osd

    @property
    def window_visible(self) -> bool:
        try:
            return self.window is not None and self.root.state() != "withdrawn"
        except tk.TclError:
            return False

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

    def report(self, section: str, text: str, ok: bool | None = None) -> None:
        self.status[section] = text
        self.status_ok[section] = ok
        if section == "speech" and ok is False and not self.window_visible:
            self.notify(APP_NAME, text)  # ошибка по горячей клавише — окно может быть скрыто
        if self.window is not None:
            self.window.refresh_status()

    # ================================================================ действия (GUI-поток)
    def apply_now(self) -> None:
        snap = self._publish()
        self.worker.submit(lambda: self._w_sync(snap, force=True), key="sync")

    def switch_profile(self, name: str, quiet: bool = False) -> None:
        if name not in self.cfg["profiles"]:
            return
        self.cfg["active_profile"] = name
        self.save_soon()
        self.settings_changed(devices=True)
        if not quiet:
            self.notify(APP_NAME, f"Профиль: {name}")
        if self.window is not None:
            self.window.refresh()

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

    def settings_changed(self, devices: bool = False, hotkeys: bool = False, sync: bool = False,
                         tools: bool = False) -> None:
        """Вызывается GUI при любом изменении настроек.

        devices — сразу перепроверить устройства/аудиопотоки; sync — перепроверить с задержкой
        (схлопывается при перетаскивании ползунка). Параметры эффектов применяются мгновенно сами.
        """
        self.save_soon()
        eng = self.engine
        if eng is not None:  # параметры звука применяются мгновенно, без потока управления
            self._push_dsp(eng, self.cfg)
        if tools:
            self.tools.sync(self.cfg["tools"])
            hotkeys = True
        if hotkeys:
            self._sync_hotkeys()
            self._sync_ptt()
        snap = self._publish()
        if devices:
            self.worker.submit(lambda: self._w_sync(snap), key="sync")
        elif sync:
            self.worker.submit(lambda: self._w_sync(snap), delay=0.3, key="sync")
        self.tray.refresh()

    def _push_dsp(self, eng, cfg) -> None:
        mic = cfg["mic"]
        eng.mic_chain.configure(mic)
        eng.spk_chain.configure(cfg["spk"])
        eng.monitor_voice = bool(mic["monitor"])
        eng.monitor_gain = mic["monitor_volume"] / 100.0
        eng.muted = self.muted

    def _publish(self) -> dict:
        """GUI-поток: обновить снимок настроек, который читают другие потоки."""
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
        guard = self.tools.get("micguard")
        if announce and not (guard is not None and guard.running and self.cfg["tools"]["micguard"]["osd"]):
            self.notify(APP_NAME, "Микрофон выключен" if muted else "Микрофон включён")
        self.tools.broadcast("on_mute", muted, announce)
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
        b = {"mute": self.cfg["mic"]["mute_hotkey"], "stop": self.cfg["soundboard"]["stop_hotkey"],
             "repeat": self.cfg["tts"]["repeat_hotkey"]}
        for i, snd in enumerate(self.cfg["soundboard"]["sounds"]):
            if snd.get("hotkey"):
                b[f"sound:{i}"] = snd["hotkey"]
        b.update(self.tools.hotkeys(self.cfg["tools"]))
        self.hotkeys.set_bindings(b)

    def _on_hotkey(self, name: str) -> None:
        if name == "mute":
            self.toggle_mute()
        elif name == "stop":
            self.stop_sounds()
        elif name == "repeat":
            self.repeat_last()
        elif name.startswith("sound:"):
            self.play_sound(int(name.split(":", 1)[1]))
        elif name.startswith("tool:"):
            self.tools.on_hotkey(name)

    # ---------------------------------------------------------------- мультитул
    def toggle_tool(self, key: str) -> None:
        cfg = self.cfg["tools"][key]
        cfg["enabled"] = not cfg["enabled"]
        self.settings_changed(tools=True)
        self.refresh_ui()

    def tool_status_changed(self, key: str) -> None:
        """Инструмент сообщил о новом состоянии (вызывается в GUI-потоке)."""
        self.tray.refresh_title()
        if self.window is not None:
            self.window.refresh_tool(key)

    def start_download_watch(self) -> None:
        from .tools.network import DownloadWatcher

        self.cancel_download_watch()
        d = self.cfg["tools"]["download"]
        self.tools.download = DownloadWatcher(self, d["action"], float(d["kbps"]), float(d["minutes"]))
        self.tools.download.start()
        self.tool_status_changed("download")
        self.tray.refresh()

    def cancel_download_watch(self) -> None:
        if self.tools.download is not None:
            self.tools.download.cancel()
            self.tool_status_changed("download")
            self.tools.download = None
            self.tray.refresh()

    @property
    def download_active(self) -> bool:
        d = self.tools.download
        return d is not None and d.state in ("waiting", "downloading", "countdown")

    def start_sleep_timer(self, minutes: float | None = None) -> None:
        from .tools.system import SleepTimer

        self.cancel_sleep_timer()
        st = self.cfg["tools"]["sleeptimer"]
        if minutes is not None:
            st["minutes"] = int(minutes)
            self.save_soon()
        self.tools.sleep_timer = SleepTimer(self, st["minutes"], st["action"], st["fade"])
        self.tools.sleep_timer.start()
        if self.window is not None:
            self.window.refresh()
        self.tool_status_changed("sleeptimer")
        self.tray.refresh()

    def cancel_sleep_timer(self) -> None:
        if self.tools.sleep_timer is not None:
            self.tools.sleep_timer.cancel()
            self.tools.sleep_timer = None
            self.tool_status_changed("sleeptimer")
            self.tray.refresh()

    @property
    def sleep_timer_active(self) -> bool:
        t = self.tools.sleep_timer
        return t is not None and t.state in ("running", "fading")

    def open_mixer(self) -> None:
        self.tools.instance("apps").open_mixer()

    def screen_text(self) -> None:
        self.tools.instance("ocr").grab()

    def pick_locksmith(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title="Какой файл не удаляется или не открывается?")
        if path:
            self.open_locksmith([path])

    def set_brightness(self, level: int) -> None:
        tool = self.tools.get("brightness")
        if tool is not None and tool.running:
            tool.set_level(level, osd=True)

    def open_locksmith(self, paths: list[str]) -> None:
        from .tools.system import LocksmithWindow

        LocksmithWindow(self.root, paths)

    def set_power_option(self, option: str) -> None:
        tool = self.tools.get("power")
        if tool is not None and tool.running:
            tool.user_choice(option)
        else:
            from .tools.system import set_power

            set_power(option)
        self.tool_status_changed("power")

    # ---------------------------------------------------------------- озвучка / саундборд
    def speak(self, text: str, remember: bool = True) -> None:
        """Озвучить текст: слышите вы (наушники) и — если работает обработка микрофона — собеседники."""
        text = text.strip()
        if not text:
            return
        tts = copy.deepcopy(self.cfg["tts"])
        if remember:
            hist = [h for h in self.cfg["tts"]["history"] if h != text]
            self.cfg["tts"]["history"] = ([text] + hist)[:30]
            self.save_soon()
        self.report("speech", "Озвучиваю…" if tts["engine"] == "sapi" else "Получаю нейроголос…")
        self._media_then_play(lambda: self._m_tts().synth(text, tts), what="Озвучка",
                              gain=tts["volume"] / 100.0, through_fx=tts["through_fx"], monitor=tts["monitor"],
                              tag="tts", queue=True)

    def speak_local(self, text: str, lang: str | None = None) -> None:
        """Прочитать вслух только для себя (в наушники): перевод, текст с экрана."""
        text = text.strip()[:3000]
        if not text:
            return
        tts = copy.deepcopy(self.cfg["tts"])
        tts["lang"] = lang
        self._media_then_play(lambda: self._m_tts().synth(text, tts), what="Чтение вслух",
                              gain=tts["volume"] / 100.0, through_fx=False, monitor=True, tag="local",
                              exclusive=True, to_mic=False)

    def repeat_last(self) -> None:
        hist = self.cfg["tts"]["history"]
        if hist:
            self.speak(hist[0], remember=False)

    def play_sound(self, index: int) -> None:
        sounds = self.cfg["soundboard"]["sounds"]
        if not 0 <= index < len(sounds):
            return
        snd, sb = sounds[index], self.cfg["soundboard"]
        name = snd.get("name") or os.path.basename(snd.get("path") or "") or "Звук"
        if snd.get("text"):
            text, tts = snd["text"], copy.deepcopy(self.cfg["tts"])
            job = lambda: self._m_tts().synth(text, tts)  # noqa: E731
            tag = "sb:text:" + text
        else:
            path = snd["path"]
            job = lambda: self._m_sounds().get(path)  # noqa: E731
            tag = "sb:file:" + path
        self._media_then_play(job, what=name, gain=sb["volume"] / 100.0, through_fx=False,
                              monitor=sb["monitor"], tag=tag, exclusive=sb["exclusive"])

    def stop_sounds(self) -> None:
        if self.engine is not None:
            self.engine.mixer.stop()
        self.report("speech", "Остановлено")

    def _m_tts(self):
        if self._tts is None:
            from .media import TTS

            self._tts = TTS()
        return self._tts

    def _m_sounds(self):
        if self._sounds is None:
            from .media import SoundCache

            self._sounds = SoundCache()
        return self._sounds

    def _media_then_play(self, job, *, what: str, **play_kw) -> None:
        def media_job():
            t0 = time.perf_counter()
            try:
                data = job()
            except Exception as err:  # noqa: BLE001
                log.warning("%s: %s", what, err)
                self.ui(self.report, "speech", f"{what}: {err}", False)
                return
            log.info("%s: готово за %.0f мс", what, (time.perf_counter() - t0) * 1000)
            self.worker.submit(lambda: self._w_play_clip(data, what, **play_kw))

        self.media.submit(media_job)

    def list_voices(self, engine: str, callback) -> None:
        """Голоса выбранного движка → callback(list[(id, название)] | None, ошибка)."""
        def job():
            try:
                if engine == "edge":
                    from .media import EdgeTTS

                    voices = EdgeTTS.voices()
                else:
                    voices = [(v, v) for v in self._m_tts().sapi.voices()]
                self.ui(callback, voices, "")
            except Exception as err:  # noqa: BLE001
                log.warning("Голоса %s: %s", engine, err)
                self.ui(callback, None, str(err))

        self.media.submit(job)

    def edge_available(self) -> bool:
        try:
            importlib.invalidate_caches()
            return importlib.util.find_spec("edge_tts") is not None
        except (ImportError, ValueError):
            return False

    # ---------------------------------------------------------------- шумоподавление: обучение
    def start_noise_learning(self) -> str | None:
        """→ None, если запись началась, иначе причина."""
        eng = self.engine
        if eng is None or not eng.mic_running:
            return "Сначала включите «Обработку микрофона» — обучение слушает ваш микрофон."
        dn = eng.mic_chain.denoiser
        if dn is None:
            return "Выберите режим «ИИ» в шумоподавлении (нейросеть должна быть запущена)."
        dn.start_learning()
        return None

    def learning_progress(self) -> float | None:
        eng = self.engine
        dn = eng.mic_chain.denoiser if eng is not None else None
        return dn.learning_progress if dn is not None else None

    def cancel_learning(self) -> None:
        eng = self.engine
        if eng is not None and eng.mic_chain.denoiser is not None:
            eng.mic_chain.denoiser.cancel_learning()

    def finish_learning(self, callback) -> bool:
        """Если запись закончена — строит профиль в потоке управления → callback(данные|None, текст)."""
        eng = self.engine
        dn = eng.mic_chain.denoiser if eng is not None else None
        rows = dn.take_learned() if dn is not None else None
        if rows is None:
            return False

        def job():
            from .denoise import NoiseProfile

            try:
                data, summary = NoiseProfile.build(*rows)
            except ValueError as err:
                self.ui(callback, None, str(err))
                return
            self.ui(callback, data, summary)

        self.worker.submit(job)
        return True

    # ---------------------------------------------------------------- тест «как меня слышат»
    def start_voice_check(self, seconds: float = 6.0) -> str | None:
        eng = self.engine
        if eng is None or not eng.mic_running:
            return "Сначала включите «Обработку микрофона»."
        eng.start_recording(seconds)
        return None

    def voice_check_progress(self) -> float | None:
        eng = self.engine
        return eng.recording_progress if eng is not None else None

    def finish_voice_check(self) -> bool:
        eng = self.engine
        rec = eng.take_recording() if eng is not None else None
        if rec is None:
            return False
        self._voice_check = rec
        return True

    def play_voice_check(self, processed: bool) -> None:
        rec = getattr(self, "_voice_check", None)
        if rec is None:
            return
        data = rec[1] if processed else rec[0]
        peak = float(abs(data).max()) or 1.0
        gain = min(4.0, 0.7 / peak)  # тихую запись делаем слышнее, громкую не перегружаем
        what = "Как слышат другие" if processed else "Без обработки"
        self.worker.submit(lambda: self._w_play_clip(data, what, gain=gain, through_fx=False, monitor=True,
                                                     tag="voicecheck", exclusive=True, to_mic=False))

    # ---------------------------------------------------------------- установка пакетов
    def install_package(self, args: list[str], done) -> None:
        """pip install в фоне (для запуска из исходников) → done(успех, вывод)."""
        if getattr(sys, "frozen", False):
            done(False, "В собранной версии (exe) пакеты встроены — скачайте свежую сборку.")
            return

        def run():
            import subprocess

            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            try:
                r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *args],
                                   capture_output=True, text=True, timeout=900, creationflags=flags)
                ok, out = r.returncode == 0, (r.stdout + r.stderr)[-3000:]
            except Exception as err:  # noqa: BLE001
                ok, out = False, str(err)
            log.info("pip install %s → %s", args, ok)
            if ok:
                importlib.invalidate_caches()
            self.ui(done, ok, out)

        threading.Thread(target=run, name="SoundFixer-pip", daemon=True).start()

    def reload_denoise(self) -> None:
        def job():
            from . import denoise

            denoise.load_library(force=True)
            self._dn_engine = None  # пересоздать нейросеть
            self._w_prepare_denoise(self._snap)
        self.worker.submit(job)
        self.settings_changed(devices=True)

    # ---------------------------------------------------------------- диагностика
    def run_diagnostics(self, callback) -> None:
        snap = self._publish()
        self.worker.submit(lambda: self._w_diagnostics(snap, callback))

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
                self.ui(self.report, "devices", f"Звуковая служба Windows недоступна: {err}", False)
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
        eng = self.engine
        if topology_changed and eng is not None and (eng.mic_running or eng.spk_running or eng.monitor_running):
            eng.reset()
            self._running_cfg = {"mic": None, "spk": None}
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
                and eng.mic_running and eng.cable_running and snap["virtual_mic"]:
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
                        self.ui(self.report, "devices", f"Ошибка назначения устройства: {err}", False)
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
    def _w_get_engine(self):
        if self.engine is None:
            from .engine import Engine  # ленивая загрузка numpy/pedalboard/PortAudio

            self.engine = Engine(self._snap["block_size"])
            self._push_dsp(self.engine, self._snap)
        return self.engine

    def _w_monitor_target(self) -> str | None:
        """Настоящие наушники/динамики: из профиля, иначе текущее устройство Windows (не виртуальное)."""
        a = self.audio
        if a is None:
            return None
        outs = a.devices(RENDER)
        by_id = {d.id.lower(): d for d in outs}
        prof = self._snap["profiles"][self._snap["active_profile"]]
        dev = by_id.get((prof["output_id"] or "").lower())
        if dev is None or dev.is_virtual:
            dev = by_id.get((a.default_id(RENDER) or "").lower())
            if dev is None or dev.is_virtual:
                dev = next((d for d in outs if not d.is_virtual), None)
        return dev.name if dev else None

    def _w_play_clip(self, data, what: str, *, gain: float, through_fx: bool, monitor: bool, tag: str,
                     exclusive: bool = False, queue: bool = False, to_mic: bool = True) -> None:
        try:
            eng = self._w_get_engine()
            to_others = to_mic and eng.mic_running and eng.cable_running
            need_monitor = monitor or not to_others
            if need_monitor:
                target = self._w_monitor_target()
                if target is None:
                    raise RuntimeError("не найдены наушники или динамики")
                eng.open_monitor(target)
            eng.mixer.play(data, gain, to_mic=to_others, through_fx=through_fx, monitor=need_monitor,
                           tag=tag, exclusive=exclusive, queue=queue)
            self._w_schedule_idle()
            secs = len(data) / 48000
            if not to_mic:  # проверка «как меня слышат» — специально только в наушники
                self.ui(self.report, "speech", f"▶ {what} ({secs:.1f} с) — в наушниках", True)
            elif to_others:
                who = "собеседники и вы" if need_monitor else "собеседники"
                self.ui(self.report, "speech", f"▶ {what} ({secs:.1f} с) — слышат {who}", True)
            else:
                hint = ("включите «Обработку микрофона» и выберите виртуальный кабель"
                        if not (eng.mic_running) else "выберите виртуальный кабель во вкладке «Настройки»")
                self.ui(self.report, "speech", f"▶ {what} ({secs:.1f} с) — слышите только вы. "
                                               f"Чтобы слышали собеседники: {hint}.", None)
        except Exception as err:  # noqa: BLE001
            log.exception(what)
            self.ui(self.report, "speech", f"{what}: не удалось воспроизвести — {err}", False)

    def _w_schedule_idle(self) -> None:
        self.worker.submit(self._w_idle_check, delay=6.0, key="idle")

    def _w_idle_check(self) -> None:
        """Выход в наушники закрывается, когда не нужен, — экономия CPU."""
        eng = self.engine
        if eng is None or not eng.monitor_running:
            return
        needed = (eng.mic_running and self._snap["mic"]["monitor"]) or eng.mixer.busy
        if needed or eng.idle_for() < 5.0:
            self._w_schedule_idle()
            return
        eng.close_monitor()
        log.info("Выход контроля закрыт (простаивал)")

    def _w_engine(self, snap: dict) -> None:
        mic_on, spk_on = snap["mic"]["enabled"], snap["spk"]["enabled"]
        eng = self.engine
        if not (mic_on or spk_on):
            if eng is not None and (eng.mic_running or eng.spk_running):
                eng.stop_mic()
                eng.stop_spk()
                self._w_schedule_idle()
            self._w_prepare_denoise(snap)  # освободить память нейросети
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
        self._w_check_tips(snap, mic_dev)
        if mic_on:
            cable, cable_capture = self._virtual_pair(snap["virtual_mic"], outs, ins) if snap["virtual_mic"] \
                else (None, None)
            if mic_dev is None:
                mic_status = "Микрофон не найден"
            elif mic_dev.is_virtual:
                mic_status = "В профиле выбран виртуальный микрофон — выберите настоящий"
            else:
                want_mic = (mic_dev.name, cable.name if cable else None)
                mic_cable_capture = cable_capture.name if cable_capture else (cable.name if cable else "")
                if cable is None:
                    mic_status = ("Виртуальный кабель не выбран: обработанный голос слышите только вы "
                                  "(включите «Слышать себя»). Установите VB-Cable и выберите его во вкладке "
                                  "«Настройки».")
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
                eng = self._w_get_engine()
            if eng is not None:
                self._push_dsp(eng, snap)
                self._w_prepare_denoise(snap)  # до запуска микрофона: нейросеть уже будет готова
                if eng.block != snap["block_size"]:
                    eng.stop()
                    eng.block = snap["block_size"]
                    self._running_cfg = {"mic": None, "spk": None}
                if not eng.healthy():
                    log.warning("Аудиопоток остановился — перезапуск")
                    eng.reset()
                    self._running_cfg = {"mic": None, "spk": None}
                if want_mic != self._running_cfg["mic"]:
                    eng.stop_mic()
                    self._running_cfg["mic"] = None
                    if want_mic:
                        try:
                            eng.start_mic(*want_mic)
                            self._running_cfg["mic"] = want_mic
                            self._overload = 0
                            self.worker.submit(self._w_check_load, delay=4.0, key="load")
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
                # «слышать себя»: выход в наушники держим открытым, пока включено
                if eng.mic_running and snap["mic"]["monitor"] and out_dev is not None:
                    target = self._w_monitor_target()
                    if target:
                        try:
                            eng.open_monitor(target)
                        except Exception as err:  # noqa: BLE001
                            mic_status = mic_status or str(err)
                elif eng.monitor_running:
                    self._w_schedule_idle()
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

    def _w_check_tips(self, snap: dict, mic_dev) -> None:
        """Подсказки о двойной обработке: «улучшения звука» Windows искажают голос вместе с нейросетью."""
        tip = ""
        if mic_dev is not None and snap["mic"]["enabled"] and snap["mic"]["denoise"] == "ai" and self.audio:
            if self.audio.enhancements_enabled(mic_dev.id):
                tip = (f"Для «{mic_dev.name}» в Windows включены «Улучшения звука» (шумодав/эффекты драйвера, "
                       f"Voice Clarity). Вместе с нейросетью они могут искажать голос — выключите: "
                       f"Параметры → Система → Звук → микрофон → Улучшения звука → Выкл.")
        if tip != self.status.get("tips"):
            self.ui(self.report, "tips", tip, None)

    def _w_prepare_denoise(self, snap: dict) -> None:
        """Создать выбранную нейросеть шумодава (до секунды — здесь, а не в аудиопотоке)
        и передать её в цепочку микрофона. Если DeepFilterNet недоступен — откат на RNNoise."""
        eng = self.engine
        if eng is None:
            return
        mic = snap["mic"]
        want = mic["denoise_engine"] if mic["enabled"] and mic["denoise"] == "ai" else None
        if want == self._dn_engine:
            return
        from . import denoise

        if want is None:
            eng.mic_chain.install_denoiser(None)
            self._dn_engine = None
            self.denoise_info = None
            self.worker.submit(self._w_free_retired, delay=1.0, key="dn_free")
            return
        title = denoise.ENGINES[want][0]
        self.ui(self.report, "denoise", f"Загружаю нейросеть {title}…", None)
        t0 = time.perf_counter()
        used, note = want, ""
        try:
            dn = denoise.Denoiser(denoise.create_backend(want))
        except Exception as err:  # noqa: BLE001
            log.warning("Шумодав %s недоступен: %s", want, err)
            if want == "rnnoise":
                self._dn_engine = want
                self.ui(self.report, "denoise", f"RNNoise: {err}", False)
                return
            try:
                dn = denoise.Denoiser(denoise.create_backend("rnnoise"))
                used, note = "rnnoise", f"{title} недоступна ({err}) — пока работает RNNoise"
            except Exception as err2:  # noqa: BLE001
                self._dn_engine = want
                self.ui(self.report, "denoise", f"{title}: {err}; RNNoise: {err2}", False)
                return
        eng.mic_chain.install_denoiser(dn)
        self._dn_engine = want
        self.denoise_info = denoise.ENGINES[used][:2]
        log.info("Шумодав: %s (загрузка %.0f мс)", used, (time.perf_counter() - t0) * 1000)
        self.ui(self.report, "denoise", note, False if note else True)
        self.worker.submit(self._w_free_retired, delay=1.0, key="dn_free")

    LIGHTER = {"dfn_ll": "dfn", "dfn": "rnnoise"}

    def _w_check_load(self) -> None:
        """Раз в 3 с, пока работает микрофон: если процессор не успевает обрабатывать звук
        (обработка блока дольше 70% его длительности), переходим на более лёгкую нейросеть."""
        eng = self.engine
        if eng is None or not eng.mic_running:
            return
        budget = self._snap["block_size"] / 48.0
        self._overload = self._overload + 1 if eng.proc_ms > 0.7 * budget else 0
        cur = self._snap["mic"]["denoise_engine"]
        if self._overload >= 3 and cur in self.LIGHTER and self._snap["mic"]["denoise"] == "ai":
            self._overload = 0
            log.warning("Процессор не успевает (%.1f мс на блок %.0f мс) — переход %s → %s",
                        eng.proc_ms, budget, cur, self.LIGHTER[cur])
            self.ui(self._downgrade_denoise, cur, self.LIGHTER[cur], eng.proc_ms)
        self.worker.submit(self._w_check_load, delay=3.0, key="load")

    def _downgrade_denoise(self, cur: str, new: str, ms: float) -> None:
        if self.cfg["mic"]["denoise_engine"] != cur:
            return
        self.cfg["mic"]["denoise_engine"] = new
        self.settings_changed(devices=True)
        names = {"dfn": "DeepFilterNet3 лёгкая", "rnnoise": "RNNoise"}
        self.notify(APP_NAME, f"Процессор не успевал ({ms:.1f} мс на 10 мс звука) — включил {names[new]}")
        self.refresh_ui()

    def _w_free_retired(self) -> None:
        eng = self.engine
        if eng is not None:
            for old in eng.mic_chain.take_retired():
                old.close()

    def _set_engine_status(self, mic: str, spk: str) -> None:
        self.status["mic"], self.status["spk"] = mic, spk
        self.refresh_ui()

    # ---------------------------------------------------------------- диагностика (поток управления)
    def _w_diagnostics(self, snap: dict, callback) -> None:
        lines: list[str] = []

        def add(ok, text):
            lines.append(("✔ " if ok else "✖ " if ok is False else "• ") + text)

        a = self.audio
        if a is None:
            add(False, "Служба звука Windows недоступна")
        else:
            ins, outs = a.devices(CAPTURE), a.devices(RENDER)
            names = {d.id: d.name for d in ins + outs}
            add(bool(ins and outs), f"Устройства: микрофонов {len(ins)}, выходов {len(outs)}")
            add(None, f"Сейчас в Windows: микрофон «{names.get(a.default_id(CAPTURE) or '', '—')}», "
                      f"звук «{names.get(a.default_id(RENDER) or '', '—')}»")
            mic_id = (snap["profiles"][snap["active_profile"]]["input_id"] or a.default_id(CAPTURE) or "")
            enh = a.enhancements_enabled(mic_id) if mic_id else None
            if enh is not None:
                add(None if enh else True, "«Улучшения звука» Windows для микрофона: " + (
                    "включены — с нейросетью шумодава лучше выключить" if enh else "выключены"))
            virt = [d.name for d in outs if d.is_virtual]
            add(bool(virt) or None, "Виртуальные кабели: " + (", ".join(virt) if virt else
                                                              "не найдены (нужны для эффектов и озвучки собеседникам)"))
        try:
            eng = self._w_get_engine()
            import sounddevice as sd

            add(True, f"Звуковой движок: {sd.get_portaudio_version()[1]}")
        except Exception as err:  # noqa: BLE001
            add(False, f"Звуковой движок не загрузился: {err}")
            self.ui(callback, "\n".join(lines))
            return

        target = self._w_monitor_target()
        try:
            if target is None:
                raise RuntimeError("нет устройства вывода")
            eng.test_tone(target)
            self._w_schedule_idle()
            add(True, f"Тестовый сигнал отправлен в «{target}» — вы должны были услышать короткий писк")
        except Exception as err:  # noqa: BLE001
            add(False, f"Вывод в наушники: {err}")

        prof = snap["profiles"][snap["active_profile"]]
        if eng.mic_running:
            lvl = eng.mic_level
            add(True, f"Микрофон в обработке, уровень {20 * math.log10(max(lvl, 1e-6)):.0f} дБ, "
                      f"обработка {eng.proc_ms:.2f} мс на блок")
        elif a is not None:
            mic = a.device(prof["input_id"], CAPTURE) if prof["input_id"] else None
            mic = mic or a.device(a.default_id(CAPTURE) or "", CAPTURE)
            if mic is None:
                add(False, "Микрофон не найден")
            else:
                try:
                    db = eng.measure_input(mic.name)
                    add(db > -75, f"Микрофон «{mic.name}»: пик {db:.0f} дБ"
                                  + (" — тишина: проверьте, не выключен ли микрофон" if db <= -75 else ""))
                except Exception as err:  # noqa: BLE001
                    add(False, f"Микрофон «{mic.name}» не открывается: {err}")

        from . import denoise
        import numpy as np

        frame = (np.random.default_rng(0).standard_normal(480) * 0.01).astype(np.float32)
        for key in ("dfn_ll", "dfn", "rnnoise"):
            title, latency = denoise.ENGINES[key][:2]
            try:
                t0 = time.perf_counter()
                dn = denoise.Denoiser(denoise.create_backend(key))
                load_ms = (time.perf_counter() - t0) * 1000
                t0 = time.perf_counter()
                for _ in range(100):
                    dn.process(frame)
                ms = (time.perf_counter() - t0) * 10
                dn.close()
                add(True, f"Шумодав {title}: {ms:.2f} мс на кадр 10 мс, задержка {latency} мс, "
                          f"загрузка {load_ms:.0f} мс")
            except Exception as err:  # noqa: BLE001
                add(False if key != "rnnoise" else None, f"Шумодав {title}: {err}")

        try:
            voices = self.media.submit(lambda: self._m_tts().sapi.voices()).result(40)
            add(bool(voices), f"Голоса Windows: {len(voices)}" + (f" ({', '.join(voices[:4])}…)" if voices else ""))
            data = self.media.submit(lambda: self._m_tts().sapi.synth("Проверка", "", 0, 0)).result(40)
            add(len(data) > 1000, f"Озвучка голосом Windows: {len(data) / 48000:.1f} с звука")
        except Exception as err:  # noqa: BLE001
            add(False, f"Голоса Windows: {err}")
        add(True if self.edge_available() else None,
            "Нейроголоса: " + ("установлены (нужен интернет)" if self.edge_available() else "не установлены"))
        if self.hotkeys.failed:
            add(False, "Не удалось назначить клавиши (заняты другой программой): " + ", ".join(self.hotkeys.failed))
        log.info("Диагностика:\n%s", "\n".join(lines))
        self.ui(callback, "\n".join(lines))


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
    threading.excepthook = lambda args: excepthook(args.exc_type, args.exc_value, args.exc_traceback)


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

    if "--locksmith" in argv:  # «Чем занят?» из меню проводника — отдельное окно, без трея и звука
        from .tools.system import run_locksmith_standalone

        i = argv.index("--locksmith")
        return run_locksmith_standalone([p for p in argv[i + 1:] if p])

    from .single import SingleInstance

    inst = SingleInstance()
    if not inst.primary:
        inst.notify_primary()
        return 0
    app = App(start_hidden=autostart.TRAY_ARG in argv)
    inst.listen(lambda: app.ui(app.show_window))
    app.run()
    return 0
